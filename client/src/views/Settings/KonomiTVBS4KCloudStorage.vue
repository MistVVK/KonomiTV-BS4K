<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:cloud-24-regular" width="24px" />
            <span class="ml-2">クラウドストレージ</span>
        </h2>
        <div class="settings__description">
            Google Drive / pCloud の接続を、この管理者アカウントに紐付けて保管します。<br>
            この画面ではログイン連携とフォルダの読み取り確認だけを行います。録画の転送・削除は行いません。
        </div>
        <v-alert v-if="!isAdmin" type="info" variant="tonal" class="mt-4">
            管理者アカウントでログインすると接続を管理できます。
        </v-alert>
        <div v-else class="settings__content">
            <div class="settings__item">
                <div class="settings__item-heading">別の PC でキーを取得</div>
                <div class="settings__item-label">
                    ブラウザを使える PC に rclone をインストールし、下記のコマンドで認証してください。<br>
                    rclone が表示するトークン JSON（波括弧を含む部分）だけを取り込みます。
                    トークン欄に設定ファイル全体やパスワードは貼り付けないでください。<br>
                    キーはこの画面では伏せ字になり、取り込み後に入力欄から消去されます。
                </div>
                <div class="konomitv-bs4k-cloud-commands">
                    <div>Google Drive（組み込みアプリ）: <code>rclone authorize drive</code></div>
                    <div>pCloud: <code>rclone authorize pcloud</code></div>
                </div>
                <div class="settings__item-label mt-3">
                    <strong>Google Drive の独自 OAuth アプリを使う場合（任意）</strong><br>
                    1. Google Cloud Console でプロジェクトを選択・作成し、Google Drive API を有効にします。<br>
                    2. Google Auth Platform で同意画面と対象ユーザーを設定します。テスト公開なら利用するアカウントをテストユーザーに追加します。<br>
                    3. OAuth クライアントを「デスクトップ アプリ」で作成し、ID とシークレットを取得します。KonomiTV の HTTPS callback は登録しません。<br>
                    4. 別 PC で <code>rclone config</code> を実行し、新規 remote の種類を <code>drive</code> にします。
                    対話欄の client_id / client_secret に取得した値を入力し、ブラウザで認証します。秘密値をコマンド引数へ書かないでください。<br>
                    5. rclone が表示する token の JSON と、同じクライアント ID / シークレットをこの画面の専用欄へ入力します。
                    テスト公開では更新トークンの期限など Google 側の制約があります。失効時は再認証してください。<br>
                    <a href="https://rclone.org/drive/#making-your-own-client-id" target="_blank" rel="noopener noreferrer">rclone 公式の取得手順</a>
                </div>
                <div class="settings__item-label mt-3">
                    pCloud は認証したアカウントの US / EU リージョンを選んでください。<br>
                    Google の共有 OAuth アプリには提供側の制限や廃止予定があります。
                    rclone 側で認証できない場合、この画面からは回避できません。
                </div>
                <v-btn color="primary" class="mt-4" :disabled="busy" @click="openImport(null)">接続を追加</v-btn>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">接続一覧</div>
                <v-progress-linear v-if="loading" indeterminate color="primary" class="my-3" />
                <p v-else-if="connections.length === 0" class="settings__item-label">接続はまだありません。</p>
                <article v-for="connection in connections" :key="connection.id" class="konomitv-bs4k-cloud-connection">
                    <h3>{{ connection.name }}</h3>
                    <p class="settings__item-label">
                        {{ connection.provider === 'GoogleDrive' ? 'Google Drive' : `pCloud (${connection.region})` }}
                        ・{{ statusLabels[connection.status] }}<br>
                        最終確認: {{ formatTime(connection.checked_at) }}<br>
                        キーの有効期限: {{ connection.expires_at ? formatTime(connection.expires_at) : '期限の指定なし' }}
                    </p>
                    <div class="konomitv-bs4k-cloud-actions">
                        <v-btn variant="tonal" :disabled="busy" @click="checkConnection(connection)">接続確認・フォルダ一覧</v-btn>
                        <v-btn variant="text" :disabled="busy" @click="openImport(connection)">キーを再取り込み</v-btn>
                        <v-btn variant="text" color="error" :disabled="busy" @click="disconnectTarget = connection">接続解除</v-btn>
                    </div>
                </article>
            </div>
        </div>

        <v-dialog v-model="importOpen" max-width="620" :persistent="busy" @after-leave="clearKey">
            <v-card>
                <v-card-title>{{ importId ? 'キーを再取り込み' : '接続を追加' }}</v-card-title>
                <v-card-text>
                    <v-text-field v-model="connectionName" label="接続名" maxlength="80" variant="outlined" :disabled="busy" />
                    <v-select v-model="provider" label="サービス" :items="providers" variant="outlined" :disabled="busy || importId !== null" />
                    <v-select v-if="provider === 'PCloud'" v-model="region" label="リージョン" :items="['US', 'EU']"
                        variant="outlined" :disabled="busy" />
                    <template v-if="provider === 'GoogleDrive'">
                        <v-text-field v-model="clientId" label="OAuth クライアント ID（任意）" autocomplete="off"
                            :spellcheck="false" variant="outlined" :disabled="busy" />
                        <v-text-field v-model="clientSecret" label="OAuth クライアント シークレット（任意）" type="password"
                            autocomplete="off" :spellcheck="false" variant="outlined" :disabled="busy" />
                        <p class="mb-4">両方空欄なら rclone 組み込みアプリを使います。独自アプリの場合はトークン取得時と同じ組を入力してください。
                            保存済みの値は再表示しません。再取り込みでも空欄は組み込みへの切り替えになります。</p>
                    </template>
                    <v-text-field v-model="tokenJson" label="rclone のトークン JSON" type="password" autocomplete="off"
                        :spellcheck="false" variant="outlined" :disabled="busy" />
                    <p>読み取りによる接続確認が成功した場合だけ保存します。再取り込みに失敗しても既存のキーは保持します。</p>
                </v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn :disabled="busy" @click="cancelImport">キャンセル</v-btn>
                    <v-btn color="primary" :loading="busy" :disabled="!connectionName.trim() || !tokenJson.trim()" @click="importKey">
                        確認して取り込む
                    </v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>

        <v-dialog :model-value="disconnectTarget !== null" max-width="520" :persistent="busy"
            @update:model-value="value => { if (!value && !busy) disconnectTarget = null; }">
            <v-card>
                <v-card-title>接続を解除しますか？</v-card-title>
                <v-card-text>このサーバーに保存したキーだけを削除します。クラウド上のフォルダやファイルは削除しません。</v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn :disabled="busy" @click="disconnectTarget = null">キャンセル</v-btn>
                    <v-btn color="error" :loading="busy" @click="disconnect">解除する</v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>

        <v-dialog v-model="foldersOpen" max-width="620">
            <v-card>
                <v-card-title>直下のフォルダ</v-card-title>
                <v-card-text>
                    <p>接続を確認しました。最大100件を表示します。</p>
                    <p v-if="folders.length === 0" class="mt-3">直下にフォルダはありません。</p>
                    <v-list v-else :items="folders" />
                </v-card-text>
                <v-card-actions><v-spacer /><v-btn @click="foldersOpen = false">閉じる</v-btn></v-card-actions>
            </v-card>
        </v-dialog>
    </SettingsBase>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import Message from '@/message';
import KonomiTVBS4KCloudStorage, { IKonomiTVBS4KCloudConnection } from '@/services/KonomiTVBS4KCloudStorage';
import useUserStore from '@/stores/UserStore';
import { dayjs } from '@/utils';
import SettingsBase from '@/views/Settings/Base.vue';

const userStore = useUserStore();
const isAdmin = computed(() => userStore.user?.is_admin === true);
const connections = ref<IKonomiTVBS4KCloudConnection[]>([]);
const loading = ref(false);
const busy = ref(false);
const importOpen = ref(false);
const importId = ref<string | null>(null);
const connectionName = ref('');
const provider = ref<'GoogleDrive' | 'PCloud'>('GoogleDrive');
const region = ref<'US' | 'EU'>('US');
// 入力中の秘密はこのcomponent内だけに保持し、store・localStorage・通知へ渡さない。
const tokenJson = ref('');
const clientId = ref('');
const clientSecret = ref('');
const disconnectTarget = ref<IKonomiTVBS4KCloudConnection | null>(null);
const foldersOpen = ref(false);
const folders = ref<string[]>([]);
const providers = [{title: 'Google Drive', value: 'GoogleDrive'}, {title: 'pCloud', value: 'PCloud'}];
const statusLabels = {Connected: '接続確認済み', NeedsCheck: '接続確認が必要', Error: '接続確認に失敗'};
const formatTime = (value: string | null) => value ? dayjs(value).format('YYYY/MM/DD HH:mm') : '未確認';

function clearKey(): void { tokenJson.value = ''; clientId.value = ''; clientSecret.value = ''; }
function cancelImport(): void { clearKey(); importOpen.value = false; }
function openImport(connection: IKonomiTVBS4KCloudConnection | null): void {
    clearKey();
    importId.value = connection?.id ?? null;
    connectionName.value = connection?.name ?? '';
    provider.value = connection?.provider ?? 'GoogleDrive';
    region.value = connection?.region ?? 'US';
    importOpen.value = true;
}

async function reload(): Promise<void> {
    loading.value = true;
    try { connections.value = await KonomiTVBS4KCloudStorage.list() ?? []; }
    finally { loading.value = false; }
}

async function importKey(): Promise<void> {
    if (busy.value) return;
    busy.value = true;
    try {
        const success = await KonomiTVBS4KCloudStorage.importKey({
            name: connectionName.value.trim(), provider: provider.value, region: region.value, token_json: tokenJson.value,
            client_id: provider.value === 'GoogleDrive' ? clientId.value : '',
            client_secret: provider.value === 'GoogleDrive' ? clientSecret.value : '',
        }, importId.value);
        if (success) {
            importOpen.value = false;
            Message.success('接続を確認してキーを保存しました。');
            await reload();
        }
    } finally {
        clearKey();
        busy.value = false;
    }
}

async function checkConnection(connection: IKonomiTVBS4KCloudConnection): Promise<void> {
    if (busy.value) return;
    busy.value = true;
    try {
        const result = await KonomiTVBS4KCloudStorage.check(connection.id);
        if (result !== null) { folders.value = result; foldersOpen.value = true; }
        await reload();
    } finally { busy.value = false; }
}

async function disconnect(): Promise<void> {
    if (busy.value || disconnectTarget.value === null) return;
    busy.value = true;
    try {
        if (await KonomiTVBS4KCloudStorage.disconnect(disconnectTarget.value.id)) {
            disconnectTarget.value = null;
            await reload();
        }
    } finally { busy.value = false; }
}

onMounted(async () => { await userStore.fetchUser(); if (isAdmin.value) await reload(); });
onBeforeUnmount(clearKey);
</script>

<style lang="scss" scoped>
.konomitv-bs4k-cloud-commands { margin-top: 16px; line-height: 2; overflow-wrap: anywhere; }
.konomitv-bs4k-cloud-connection { padding: 20px 0; border-bottom: 1px solid rgba(var(--v-theme-on-surface), 0.12); }
.konomitv-bs4k-cloud-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
</style>
