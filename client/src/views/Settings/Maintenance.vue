<template>
    <component :is="embedded ? 'div' : SettingsBase">
        <h2 class="settings__heading" v-if="embedded === false">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:wrench-settings-20-filled" width="22px" />
            <span class="ml-2">{{section_title}}</span>
        </h2>
        <div class="settings__description" v-if="embedded === false">
            KonomiTV サーバーの保守操作を実行します。管理者アカウントでログインしている必要があります。<br>
        </div>

        <div class="settings__content" v-if="isSectionVisible('logs')"
            :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="fluent:document-text-16-regular" width="22px" />
                <span class="ml-2">ログ</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">サーバーログの表示</div>
                <div class="settings__item-label">
                    KonomiTV サーバーの動作ログとアクセスログをリアルタイムで表示します。<br>
                    サーバーの動作状況の確認やトラブルシューティングに役立ちます。<br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="server_log_dialog = !server_log_dialog">
                <Icon icon="fluent:document-text-16-regular" height="20px" />
                <span class="ml-2">サーバーログを表示</span>
            </v-btn>
        </div>

        <div class="settings__content" v-if="isSectionVisible('database')"
            :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="iconoir:database-backup" width="22px" />
                <span class="ml-2">DB・録画</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">KonomiTV のデータベースを更新</div>
                <div class="settings__item-label">
                    KonomiTV のデータベースに保存されている、チャンネル情報・番組情報・Twitter アカウント情報などの外部 API に依存するデータをすべて更新します。<br>
                    即座に外部 API からのデータ更新を反映させたいときに利用してください。<br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="updateDatabase()">
                <Icon icon="iconoir:database-backup" height="20px" />
                <span class="ml-2">データベースを更新</span>
            </v-btn>
            <div class="settings__item">
                <div class="settings__item-heading">録画フォルダの一括スキャンを手動実行</div>
                <div class="settings__item-label">
                    録画フォルダ内のファイルは、通常 KonomiTV サーバーの起動時に自動的にスキャンされます。<br>
                    録画ファイルが KonomiTV に正しく反映されていない場合にのみ実行してみてください。<br>
                </div>
                <div class="settings__item-label mt-1">
                    <strong>大量の録画ファイルが保存されている環境では、処理完了まで数時間〜数日以上かかることがあります。</strong><br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="runBatchScan()">
                <Icon icon="fluent:folder-sync-20-regular" height="20px" />
                <span class="ml-2">録画フォルダの一括スキャンを手動実行</span>
            </v-btn>
        </div>

        <div class="settings__content" v-if="isSectionVisible('analysis')"
            :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="fluent:scan-dash-20-regular" width="22px" />
                <span class="ml-2">解析</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">すべての録画ファイルのメタデータを再解析</div>
                <div class="settings__item-label">
                    KonomiTV に登録されているすべての録画ファイルのメタデータを強制的に再解析します。<br>
                    メタデータの解析方法が変更された後に、既存の録画ファイルにも新しい解析結果を反映したい場合に利用してください。<br>
                </div>
                <div class="settings__item-label mt-1">
                    <strong>すべての録画ファイルを読み込むため、処理完了まで数時間〜数日以上かかることがあります。</strong><br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="reanalyzeAllRecordedVideos()">
                <Icon icon="fluent:video-clip-20-filled" height="20px" />
                <span class="ml-2">すべての録画ファイルを再解析</span>
            </v-btn>
            <div class="settings__item">
                <div class="settings__item-heading">すべての録画ファイルの CM 区間を再判定</div>
                <div class="settings__item-label">
                    KonomiTV に登録されているすべての録画ファイルについて、既存結果を上書きして CM 区間を再判定します。<br>
                    CM 判定方法が変更された後に、既存の録画ファイルにも新しい判定結果を反映したい場合に利用してください。<br>
                </div>
                <div class="settings__item-label mt-1">
                    <strong>すべての録画ファイルを読み込むため、処理完了まで数時間〜数日以上かかることがあります。</strong><br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="detectCMSectionsForAllRecordedVideos()">
                <Icon icon="fluent:scan-dash-20-regular" height="20px" />
                <span class="ml-2">すべての CM 区間を再判定</span>
            </v-btn>
            <div class="settings__item">
                <div class="settings__item-heading">録画ファイルのバックグラウンド解析タスクを再実行</div>
                <div class="settings__item-label">
                    録画ファイルのメタデータ解析やサムネイル作成が完了していない場合に、これらの処理を再度実行します。<br>
                    PC のシャットダウンなどで途中で中断してしまった場合は、このボタンから処理を再開できます。<br>
                </div>
                <div class="settings__item-label mt-1">
                    <strong>大量の録画ファイルが保存されている環境では、処理完了まで数時間〜数日以上かかることがあります。</strong><br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="startBackgroundAnalysis()">
                <Icon icon="fluent:book-arrow-clockwise-20-regular" height="20px" />
                <span class="ml-2">バックグラウンド解析タスクを再実行</span>
            </v-btn>
            <div class="settings__item">
                <div class="settings__item-heading">共有 CM ロゴを再スキャン</div>
                <div class="settings__item-label">
                    共有ロゴフォルダを再スキャンし、追加・変更・削除された .lgd ファイルを KonomiTV に反映します。<br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-5" color="background-lighten-2" variant="flat"
                @click="rescanCMLogos()">
                <Icon icon="fluent:image-sync-20-filled" height="20px" />
                <span class="ml-2">共有 CM ロゴを再スキャン</span>
            </v-btn>
        </div>

        <div class="settings__content" v-if="isSectionVisible('server')"
            :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading">
                <Icon icon="fluent:power-20-filled" width="22px" />
                <span class="ml-2">サーバー操作</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading text-error-lighten-1">KonomiTV サーバーを再起動</div>
                <div class="settings__item-label">
                    KonomiTV サーバーを再起動します。サーバー設定の変更を反映するには再起動が必要です。<br>
                    <strong>再起動を実行すると、すべての視聴中セッションが切断されます。</strong>十分注意してください。<br>
                </div>
            </div>
            <v-btn class="settings__save-button bg-error mt-5" variant="flat" @click="restartServer()">
                <Icon icon="fluent:arrow-counterclockwise-20-filled" height="20px" />
                <span class="ml-2">KonomiTV サーバーを再起動</span>
            </v-btn>
            <div class="settings__item">
                <div class="settings__item-heading text-error-lighten-1">KonomiTV サーバーをシャットダウン</div>
                <div class="settings__item-label">
                    KonomiTV サーバーをシャットダウンします。<br>
                    <strong>シャットダウンを実行すると、再度手動で KonomiTV サーバーを起動するまで KonomiTV にアクセスできなくなります。</strong>十分注意してください。<br>
                </div>
                <div class="settings__item-label mt-1">
                    なお、Linux 版 KonomiTV サーバーはプロセス管理を PM2 / Docker に委譲しているため、シャットダウン後は自動で再起動されます。完全にシャットダウンするには、PM2 / Docker 側でサービスを停止してください。<br>
                </div>
            </div>
            <v-btn class="settings__save-button bg-error mt-5" variant="flat" @click="shutdownServer()">
                <Icon icon="fluent:power-20-filled" height="20px" />
                <span class="ml-2">KonomiTV サーバーをシャットダウン</span>
            </v-btn>
        </div>

        <ServerLogDialog :modelValue="server_log_dialog" @update:modelValue="server_log_dialog = $event" />
    </component>
</template>

<script lang="ts" setup>

import { computed, ref } from 'vue';

import ServerLogDialog from '@/components/Settings/ServerLogDialog.vue';
import Message from '@/message';
import CMAnalysis from '@/services/CMAnalysis';
import Maintenance from '@/services/Maintenance';
import Version from '@/services/Version';
import useUserStore from '@/stores/UserStore';
import Utils from '@/utils';
import SettingsBase from '@/views/Settings/Base.vue';

type MaintenanceSection = 'maintenance' | 'logs' | 'database' | 'analysis' | 'server' | 'all';

const props = withDefaults(defineProps<{
    section?: MaintenanceSection;
    embedded?: boolean;
}>(), {
    section: 'all',
    embedded: false,
});

const embedded = computed(() => props.embedded);
const section_title = computed(() => ({
    maintenance: '診断・データ保守',
    logs: 'ログ',
    database: 'DB・録画',
    analysis: '解析',
    server: 'サーバー操作',
    all: 'メンテナンス',
})[props.section]);

function isSectionVisible(target_section: Exclude<MaintenanceSection, 'all'>): boolean {
    if (props.section === 'all' || props.section === target_section) {
        return true;
    }
    return props.section === 'maintenance' && ['logs', 'database', 'analysis'].includes(target_section);
}

// 管理者権限が確認できるまでは、保守操作を無効化する
const is_disabled = ref(true);
const user_store = useUserStore();
user_store.fetchUser().then((user) => {
    if (user && user.is_admin) {
        is_disabled.value = false;
    }
});

// サーバーログダイアログの表示状態
const server_log_dialog = ref(false);

// データベースを更新する関数
async function updateDatabase() {
    Message.show('データベースを更新しています...');
    await Maintenance.updateDatabase();
    Message.success('データベースを更新しました。');
}

// 録画フォルダの一括スキャンを実行する関数
async function runBatchScan() {
    Message.info(
        '録画フォルダの一括スキャンを開始しています...\n' +
        '大量の録画ファイルが保存されている環境では、処理完了まで数時間〜数日以上かかることがあります。'
    );
    const result = await Maintenance.runBatchScan();
    if (result === true) {
        Message.success(
            '録画フォルダの一括スキャンが完了しました。\n' +
            'すべての録画ファイルがデータベースに同期されているはずです。'
        );
    }
}

// すべての録画ファイルのメタデータを再解析する関数
async function reanalyzeAllRecordedVideos() {
    Message.info(
        'すべての録画ファイルのメタデータ再解析を開始しています...\n' +
        '大量の録画ファイルが保存されている環境では、処理完了まで数時間〜数日以上かかることがあります。'
    );
    const result = await Maintenance.reanalyzeAllRecordedVideos();
    if (result === true) {
        Message.success('すべての録画ファイルのメタデータ再解析が完了しました。');
    }
}

// すべての録画ファイルの CM 区間を再判定する関数
async function detectCMSectionsForAllRecordedVideos() {
    Message.info(
        'すべての録画ファイルの CM 区間判定を開始しています...\n' +
        '大量の録画ファイルが保存されている環境では、処理完了まで数時間〜数日以上かかることがあります。'
    );
    const result = await Maintenance.detectCMSectionsForAllRecordedVideos();
    if (result === true) {
        Message.success('すべての録画ファイルの CM 区間判定が完了しました。');
    }
}

// バックグラウンド解析タスクを開始する関数
async function startBackgroundAnalysis() {
    Message.info(
        'バックグラウンド解析タスクを開始しています...\n' +
        '大量の録画ファイルが保存されている環境では、処理完了まで数時間〜数日以上かかることがあります。'
    );
    const result = await Maintenance.startBackgroundAnalysis();
    if (result === true) {
        Message.success(
            'バックグラウンド解析タスクの実行が完了しました。\n' +
            'すべての録画番組のメタデータ解析/サムネイル生成が完了しているはずです。'
        );
    }
}

// 共有 CM ロゴフォルダを再スキャンする関数
async function rescanCMLogos() {
    Message.show('共有 CM ロゴを再スキャンしています...');
    const logos = await CMAnalysis.fetchLogos(true);
    if (logos !== null) {
        Message.success(`共有 CM ロゴを再スキャンしました。（${logos.length} 件）`);
    }
}

// KonomiTV サーバーの再起動を行う関数
async function restartServer() {
    const result = await Maintenance.restartServer();
    if (result === true) {
        Message.show('KonomiTV サーバーを再起動しています...');
        // バージョン情報が取得できるようになるまで待つ
        await Utils.sleep(1.0);
        while (await Version.fetchServerVersion(true) === null) {
            await Utils.sleep(1.0);
        }
        Message.success('KonomiTV サーバーを再起動しました。');
    }
}

// KonomiTV サーバーのシャットダウンを行う関数
async function shutdownServer() {
    const result = await Maintenance.shutdownServer();
    if (result === true) {
        Message.success('KonomiTV サーバーをシャットダウンしました。');
    }
}

</script>
