<template>
    <section class="settings__item">
        <div class="settings__item-heading">暗号化鍵の管理</div>
        <div class="settings__item-label">
            クラウドへ録画を暗号化して保管・再生するための rclone crypt 鍵（パスワードおよびソルトの一組）を管理します。
            鍵は製品が自動生成し、OAuth 認証情報とは独立して保管されます。
            鍵とバックアップの両方を失った場合、クラウド上の暗号化録画は二度と復号できなくなります（OAuth の再認証だけでは復元できません）。
            別サーバーと鍵を共有する場合は、登録済みの鍵を取り出すか、削除後に同じ暗号化鍵文字列を投入してください。
        </div>
        <p class="settings__item-label mt-3">旧バージョンで保存された JSON 形式のバックアップは投入できません。
            現在のサーバーに鍵が登録されている状態であれば、鍵を削除することなく「鍵を取り出す」から新しい単一文字列の形式で再取得できます。</p>
        <div v-if="loaded && !key.key_present" class="konomitv-bs4k-crypt-actions">
            <v-btn color="primary" :disabled="busy" @click="createKeys">鍵を新規生成</v-btn>
            <v-btn variant="tonal" :disabled="busy" @click="importOpen = true">鍵を投入</v-btn>
        </div>
        <v-progress-linear v-if="busy" indeterminate color="primary" class="my-3" />
        <article v-if="loaded && key.key_present" class="konomitv-bs4k-crypt-region">
            <template v-if="key.legacy_migration_required">
                <v-chip class="my-2" color="warning" size="small">移行が必要</v-chip>
                <v-alert type="warning" variant="tonal" title="旧形式の暗号化鍵の整理が必要です">
                    <p>旧バージョンで作成された暗号化鍵が残っているため、現在の単一鍵構成への移行が必要です。
                        移行を完了するまで、暗号化鍵の平文取り出し・新規登録・バックアップ確認は利用できません。</p>
                    <p class="mt-3">本サーバーに保存されている旧形式の鍵を一括削除した上で、
                        共有する単一の暗号化鍵文字列を登録してください。</p>
                </v-alert>
            </template>
            <template v-else>
                <v-chip class="my-2" :color="key.backup_confirmed ? 'success' : 'warning'" size="small">
                    {{ key.backup_confirmed ? 'バックアップ確認済み' : 'バックアップ未確認' }}
                </v-chip>
                <div class="settings__item-label" v-if="key.backup_confirmed">暗号化鍵の保管完了が確認されています。</div>
                <div class="settings__item-label" v-else>
                    暗号化鍵の外部保管がまだ確認されていません。鍵とバックアップの両方を失った場合、
                    クラウド上の該当領域にある暗号化録画は二度と復号できなくなります（OAuth の再認証だけでは復元できません）。
                    安全な保管が完了するまで、ローカルの録画原本を削除しないでください。
                </div>
                <v-btn class="mt-3" variant="tonal" :disabled="busy" @click="openExtract">鍵を取り出す</v-btn>
            </template>
            <v-btn class="mt-3 ml-2" variant="text" color="error" :disabled="busy" @click="openDelete">
                {{ key.legacy_migration_required ? '旧形式の鍵を一括削除' : '鍵を削除' }}
            </v-btn>
        </article>
    </section>

    <v-dialog v-model="extractOpen" max-width="700" :persistent="busy">
        <v-card>
            <v-card-title>暗号化鍵の取り出し</v-card-title>
            <v-card-text>
                <v-alert type="warning" variant="tonal" class="mb-4">
                    【警告】この操作で表示される文字列は Base64 形式でエンコードされたものであり、暗号化されていません。
                    パスワードおよびソルトそのものと同じ秘密情報です。第三者から閲覧されない安全な保管先へ保存し、チャット・ログ・公開リポジトリ等へ貼り付けないでください。
                </v-alert>
                <p>表示された鍵は、第三者から閲覧されない安全なパスワードマネージャー等の保管先へ確実に保存してください。
                    チャット・ログ・公開リポジトリ等の公開場所に貼り付けないでください。</p>
                <p class="mt-3">この鍵を知る人物が該当領域の暗号化データを入手した場合、その領域内の録画データを復号できます
                    （他の暗号化領域や、クラウドアカウント自体のアクセス権を得るわけではありません）。</p>
                <p class="mt-3 mb-4">安全な場所へのバックアップが完了するまで、移動元の録画原本をローカルから削除しないでください。</p>
                <template v-if="plaintext !== null">
                    <v-text-field :model-value="plaintext" label="暗号化鍵 (Base64)" readonly
                        autocomplete="off" :spellcheck="false" variant="outlined" />
                    <v-checkbox v-model="backupAcknowledged" :disabled="busy" hide-details
                        label="表示された暗号化鍵文字列を安全な場所へ保管しました" />
                </template>
            </v-card-text>
            <v-card-actions>
                <v-spacer />
                <v-btn :disabled="busy" @click="closeExtract">閉じる</v-btn>
                <v-btn v-if="plaintext === null" color="primary" :loading="busy" @click="extractKeys">平文で鍵を表示する</v-btn>
                <v-btn v-else color="primary" :disabled="!backupAcknowledged" :loading="busy" @click="confirmBackup">保管完了を確認</v-btn>
            </v-card-actions>
        </v-card>
    </v-dialog>

    <v-dialog v-model="importOpen" max-width="700" :persistent="busy">
        <v-card>
            <v-card-title>暗号化鍵の投入</v-card-title>
            <v-card-text>
                <p class="mb-4">別インスタンスで同じ暗号化領域を共有する場合に使用します。
                    共有元の暗号化鍵から取り出した単一の鍵文字列を入力してください。
                    すでに鍵が登録されている場合は、先に現在の鍵を削除する必要があります。</p>
                <v-text-field v-model="importKey" label="暗号化鍵 (Base64)" type="password" :disabled="busy"
                    autocomplete="off" :spellcheck="false" variant="outlined"
                    hint="取り出した暗号化鍵の Base64 文字列を入力してください（JSON の編集や個別入力は不要です）。" persistent-hint />
            </v-card-text>
            <v-card-actions>
                <v-spacer />
                <v-btn :disabled="busy" @click="closeImport">キャンセル</v-btn>
                <v-btn color="primary" :loading="busy" :disabled="!importKey" @click="insertKeys">鍵を登録する</v-btn>
            </v-card-actions>
        </v-card>
    </v-dialog>
    <v-dialog v-model="deleteOpen" max-width="700" :persistent="busy">
        <v-card>
            <v-card-title>{{ deleteLegacy ? '旧形式の暗号化鍵の一括削除' : '暗号化鍵の削除' }}</v-card-title>
            <v-card-text>
                <template v-if="deleteLegacy">
                    <v-alert type="warning" variant="tonal" class="mb-4">
                        【警告】この操作は、本サーバーに保存されているすべての旧形式の暗号化鍵と、
                        その取り出し・バックアップ確認状態を一括して完全に削除します。
                    </v-alert>
                    <p>クラウド上に保存されている録画ファイルや、クラウドアカウントの OAuth 認証情報は削除されません。
                        また、他サーバーの鍵や外部に保管されたバックアップにも影響しません。</p>
                    <p class="mt-3">削除対象の鍵に対応する外部バックアップがない暗号化録画は、二度と復号できなくなります。
                        削除後に新しい鍵を生成・投入しても、以前の録画データを復元することはできません。</p>
                    <p class="mt-3">一括削除が完了すると未登録状態に戻り、共有する単一の暗号化鍵を投入できるようになります。
                        処理が途中で中断した場合は、画面を再読み込みして最新の状態を確認した上で、残りの削除を再試行してください。</p>
                </template>
                <template v-else>
                    <v-alert type="warning" variant="tonal" class="mb-4">
                        【警告】この操作は、本サーバーに保存されている暗号化鍵（パスワードおよびソルト）と、バックアップ確認状態を完全に削除します。
                    </v-alert>
                    <p>クラウド上に保存されているファイルや、クラウドアカウントの OAuth 認証情報は削除されません。</p>
                    <p class="mt-3">削除した鍵で暗号化された録画データは、外部に安全にバックアップされた同一の鍵がない限り、二度と復号できなくなります。
                        別の鍵を新しく再生成しても復元することはできません。</p>
                    <p class="mt-3">別のサーバーと共有する鍵へ入れ替える場合を除き、この操作を行わないでください。</p>
                </template>
                <v-checkbox v-model="deleteAcknowledged" :disabled="busy" hide-details
                    :label="deleteLegacy
                        ? '外部バックアップのない旧暗号化録画が復号不能になるリスクを理解し、一括削除を実行します'
                        : '鍵の削除により、バックアップのない暗号化録画が復号不能になるリスクを理解しました'" />
            </v-card-text>
            <v-card-actions>
                <v-spacer />
                <v-btn :disabled="busy" @click="deleteOpen = false">キャンセル</v-btn>
                <v-btn color="error" :disabled="!deleteAcknowledged" :loading="busy" @click="deleteKeys">
                    {{ deleteLegacy ? '旧鍵を一括削除する' : '鍵を削除する' }}
                </v-btn>
            </v-card-actions>
        </v-card>
    </v-dialog>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';

import KonomiTVBS4KCloudCryptKeys, {
    IKonomiTVBS4KCloudCryptKeyInfo,
    IKonomiTVBS4KCloudCryptKeySnapshot,
} from '@/services/KonomiTVBS4KCloudCryptKeys';

const key = ref<IKonomiTVBS4KCloudCryptKeyInfo>({key_present: false, backup_confirmed: false, legacy_migration_required: false});
const revision = ref('');
const loaded = ref(false);
const busy = ref(false);
const extractOpen = ref(false);
const extractRevision = ref('');
// 平文はこのダイアログの生存期間だけに限定し、永続Storeへ渡さない。
const plaintext = ref<string | null>(null);
const backupAcknowledged = ref(false);
const importOpen = ref(false);
const importKey = ref('');
const deleteOpen = ref(false);
const deleteAcknowledged = ref(false);
const deleteRevision = ref('');
const deleteLegacy = ref(false);
let disposed = false;

function closeExtract(): void {
    plaintext.value = null;
    backupAcknowledged.value = false;
    extractOpen.value = false;
}
function closeImport(): void {
    importKey.value = '';
    importOpen.value = false;
}
watch(extractOpen, open => { if (!open) closeExtract(); });
watch(importOpen, open => { if (!open) closeImport(); });
watch(deleteOpen, open => { if (!open) deleteAcknowledged.value = false; });

function openDelete(): void {
    closeExtract();
    deleteAcknowledged.value = false;
    deleteRevision.value = revision.value;
    deleteLegacy.value = key.value.legacy_migration_required;
    deleteOpen.value = true;
}

async function deleteKeys(): Promise<void> {
    if (busy.value || !deleteAcknowledged.value) return;
    busy.value = true;
    try {
        await KonomiTVBS4KCloudCryptKeys.request('DELETE', '', undefined, deleteRevision.value);
        deleteOpen.value = false;
        if (!disposed) await refreshKeys();
    } finally { busy.value = false; }
}

function openExtract(): void {
    closeExtract();
    extractRevision.value = revision.value;
    extractOpen.value = true;
}

async function refreshKeys(): Promise<void> {
    const result = await KonomiTVBS4KCloudCryptKeys.request<IKonomiTVBS4KCloudCryptKeySnapshot>('GET');
    if (!disposed) {
        loaded.value = result !== null;
        if (result !== null) { key.value = result; revision.value = result.revision; }
    }
}

async function createKeys(): Promise<void> {
    if (busy.value) return;
    busy.value = true;
    try {
        const result = await KonomiTVBS4KCloudCryptKeys.request<IKonomiTVBS4KCloudCryptKeyInfo>('POST');
        if (!disposed && result !== null) await refreshKeys();
    } finally { busy.value = false; }
}

async function extractKeys(): Promise<void> {
    if (busy.value) return;
    busy.value = true;
    try {
        const result = await KonomiTVBS4KCloudCryptKeys.request<string>(
            'POST', '/extract', undefined, extractRevision.value,
        );
        if (!disposed && extractOpen.value) plaintext.value = result;
        if (result === null) { closeExtract(); if (!disposed) await refreshKeys(); }
    } finally { busy.value = false; }
}

async function confirmBackup(): Promise<void> {
    if (busy.value || !backupAcknowledged.value || plaintext.value === null) return;
    busy.value = true;
    try {
        await KonomiTVBS4KCloudCryptKeys.request<IKonomiTVBS4KCloudCryptKeyInfo>(
            'POST', '/confirm-backup', undefined, extractRevision.value,
        );
        closeExtract();
        if (!disposed) await refreshKeys();
    } finally { busy.value = false; }
}

async function insertKeys(): Promise<void> {
    if (busy.value) return;
    busy.value = true;
    try {
        const result = await KonomiTVBS4KCloudCryptKeys.request<IKonomiTVBS4KCloudCryptKeyInfo>('POST', '/insert', importKey.value);
        if (result !== null) { closeImport(); if (!disposed) await refreshKeys(); }
    } finally { importKey.value = ''; busy.value = false; }
}

onMounted(refreshKeys);
onBeforeUnmount(() => { disposed = true; closeExtract(); closeImport(); });
</script>

<style lang="scss" scoped>
.konomitv-bs4k-crypt-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }
.konomitv-bs4k-crypt-region { margin-top: 20px; padding-top: 16px; border-top: 1px solid rgba(var(--v-theme-on-surface), .12); }
</style>
