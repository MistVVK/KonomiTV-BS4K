<template>
    <v-dialog v-model="show" max-width="680" :persistent="busy">
        <v-card>
            <v-card-title class="pt-6 px-6">{{ isCloud ? '録画のローカル移動' : '録画のクラウド移動' }}</v-card-title>
            <v-card-text>
                <v-progress-linear v-if="loading" indeterminate color="primary" />
                <template v-else-if="currentProgram">
                    <v-alert v-if="keyRevision === null" type="warning" variant="tonal" class="mb-3">クラウド利用不可</v-alert>
                    <p class="mb-4 font-weight-bold">{{ currentProgram.title }}</p>
                    <template v-if="isCloud">
                        <p>クラウド上の暗号化録画を取得・復号し、ローカルストレージへ移動します。</p>
                        <p class="mt-2">処理は「取得 → 検証 → ローカル公開 → クラウド側の元データ削除」の順に行われます（二重保存ではなく移動操作です）。</p>
                        <p class="mt-2">ローカルへの移動が完了するとクラウド側の原本は削除され、同じクラウド領域を共有する別サーバーの一覧からも削除が反映されます。</p>
                        <v-select v-model="restoreFolder" :items="folders" label="復元先フォルダ" class="mt-4"
                            variant="outlined" :disabled="busy || submitted" placeholder="復元先の録画フォルダを選択してください。" />
                        <v-alert v-if="folders.length === 0" type="warning" variant="tonal" class="mb-3">
                            サーバーに設定された録画フォルダが存在しないため、ローカルへ移動できません。設定画面から録画フォルダを追加してください。
                        </v-alert>
                        <p>復元先に同名のファイルが既に存在する場合、上書きは行われず処理が失敗します。</p>
                    </template>
                    <template v-else>
                        <p>選択した完成済み録画を暗号化し、設定されたクラウド保存先へ移動します。転送データの検証と移動先での読み取り確認が完了した後に、ローカルの原本ファイルが削除されます。暗号鍵（パスワードおよびソルト）を紛失した場合、クラウドアカウントへ再ログインしても録画を復号することはできません。</p>
                        <p class="mt-2">データの整合性を検証してから原本を削除するため、アップロードに加えてクラウドからの全量ダウンロード照合が発生します。ご利用の通信回線やクラウドサービスにおける通信量制限・契約内容を事前にご確認ください。</p>
                        <v-alert v-if="keyRevision !== null && !keyBackupConfirmed" type="warning" variant="tonal" class="mt-4">
                            現在の暗号化鍵のバックアップ確認が完了していません。録画をクラウドへ移動するには、「KonomiTV-BS4K クラウドストレージ設定」を開いて暗号化鍵を取り出し、安全な場所へ保管した上でバックアップ確認を完了してください。確認が完了するまでクラウドへの移動は開始できません。
                        </v-alert>
                        <v-checkbox v-model="backupConfirmed" class="mt-4"
                            :disabled="busy || submitted || !keyBackupConfirmed" hide-details
                            label="暗号鍵（パスワードおよびソルト）を取り出して安全な場所へ保管済みであることを確認しました" />
                    </template>
                </template>
            </v-card-text>
            <v-card-actions class="px-6 pb-6">
                <v-spacer />
                <v-btn variant="text" :disabled="busy" @click="show = false">キャンセル</v-btn>
                <v-btn color="primary" variant="flat" :loading="busy" :disabled="!canStart" @click="start">
                    {{ isCloud ? 'ローカルへ移動を開始' : 'クラウドへ移動を開始' }}
                </v-btn>
            </v-card-actions>
        </v-card>
    </v-dialog>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';

import Message from '@/message';
import KonomiTVBS4KCloudCryptKeys, { IKonomiTVBS4KCloudCryptKeySnapshot } from '@/services/KonomiTVBS4KCloudCryptKeys';
import KonomiTVBS4KCloudTransfers, { IKonomiTVBS4KCloudTransferRequest } from '@/services/KonomiTVBS4KCloudTransfers';
import Videos, { IRecordedProgram } from '@/services/Videos';

const props = defineProps<{ program: IRecordedProgram }>();
const show = defineModel<boolean>('show', { required: true });
const currentProgram = ref<IRecordedProgram | null>(null);
const loading = ref(false);
const busy = ref(false);
const backupConfirmed = ref(false);
const restoreFolder = ref<string | null>(null);
const folders = ref<string[]>([]);
const request = ref<IKonomiTVBS4KCloudTransferRequest | null>(null);
const submitted = ref(false);
const keyRevision = ref<string | null>(null);
const keyBackupConfirmed = ref(false);
let loadGeneration = 0;
const isCloud = computed(() => currentProgram.value?.recorded_video.storage_location === 'Cloud');
const canStart = computed(() => !loading.value && keyRevision.value !== null && currentProgram.value !== null &&
    currentProgram.value.recorded_video.status === 'Recorded' &&
    (isCloud.value ? restoreFolder.value !== null : keyBackupConfirmed.value && backupConfirmed.value));

watch(show, async (opened) => {
    const generation = ++loadGeneration;
    if (!opened) return;
    const programId = props.program.id;
    loading.value = true;
    backupConfirmed.value = false;
    restoreFolder.value = null;
    submitted.value = false;
    request.value = null;
    folders.value = [];
    keyRevision.value = null;
    keyBackupConfirmed.value = false;
    const snapshot = await KonomiTVBS4KCloudCryptKeys.request<IKonomiTVBS4KCloudCryptKeySnapshot>('GET');
    if (generation !== loadGeneration) return;
    if (snapshot?.key_present && !snapshot.legacy_migration_required) {
        keyRevision.value = snapshot.revision;
        keyBackupConfirmed.value = snapshot.backup_confirmed;
    }
    const program = await Videos.fetchVideo(programId);
    if (generation !== loadGeneration) return;
    currentProgram.value = program;
    if (isCloud.value) {
        const result = await KonomiTVBS4KCloudTransfers.restoreFolders();
        if (generation !== loadGeneration) return;
        if (result.type === 'success') folders.value = result.data;
    }
    loading.value = false;
}, {immediate: true});

async function start() {
    if (!canStart.value || busy.value || keyRevision.value === null || currentProgram.value === null) return;
    // 応答喪失後も同じ要求を再送する。入力は最初の送信時に固定して逆方向へ解釈させない。
    request.value ??= {
        request_id: crypto.randomUUID(),
        recorded_program_id: currentProgram.value.id,
        direction: isCloud.value ? 'ToLocal' : 'ToCloud',
        key_backup_confirmed: backupConfirmed.value,
        key_revision: keyRevision.value,
        restore_folder: restoreFolder.value,
    };
    submitted.value = true;
    busy.value = true;
    try {
        const result = await KonomiTVBS4KCloudTransfers.create(request.value);
        if (result.type === 'error') {
            Message.error(result.status === 409 && result.data.detail === 'Crypt key confirmation changed.' ?
                '暗号化鍵またはバックアップ確認の状態が変更されたため、移動を開始できませんでした。移動ダイアログを開き直し、最新の鍵状態を確認した上で再度実行してください。' : 'クラウド利用不可');
            return;
        }
        Message.success(isCloud.value ? '待機中' : '録画のクラウド移動を受け付けました。バックグラウンドで移動処理を開始します。');
        show.value = false;
    } finally {
        busy.value = false;
    }
}

onBeforeUnmount(() => { loadGeneration++; });
</script>
