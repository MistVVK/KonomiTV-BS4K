<template>
    <div class="settings__item">
        <div class="settings__item-heading">クラウド録画目録の同期</div>
        <p class="settings__item-label">クラウドストレージ上の録画目録や変更・削除記録を取得し、本サーバーの録画一覧を最新状態に更新します。録画ファイル本体をダウンロードする操作ではありません。</p>
        <v-btn class="my-3" variant="tonal" :loading="busy" @click="sync">目録を同期</v-btn>
        <v-alert v-if="syncLoadFailed" type="error" variant="tonal">クラウド目録の同期に失敗しました。通信切断等により最新の目録を取得できていない可能性があります。</v-alert>
        <div v-for="item in syncStates" :key="item.id" class="d-flex align-center flex-wrap ga-2 my-2">
            <span>{{ item.connection_name }} · {{ item.folder }} · {{ item.connection_id.slice(0, 8) }}</span>
            <v-chip size="small" :color="item.status === 'Failed' ? 'error' : 'primary'" variant="tonal">
                {{ item.status === 'Running' ? '目録を同期中…' : statusLabels[item.status] }}
            </v-chip>
        </div>
    </div>
    <div class="settings__item">
        <div class="settings__item-heading">録画移動処理一覧</div>
        <p class="settings__item-label">移動処理は移動先への公開が始まる前まで取り消すことができます。公開開始後の処理は取り消しできず、失敗時は同じ移動の再開のみ可能です。</p>
        <v-btn class="my-3" variant="tonal" :loading="loading" @click="load">再読み込み</v-btn>
        <v-alert v-if="loadFailed" type="error" variant="tonal">移動処理一覧の取得に失敗しました。</v-alert>
        <p v-else-if="!loading && jobs.length === 0" class="settings__item-label">現在処理中または過去の移動処理はありません。</p>
        <article v-for="job in jobs" :key="job.id"
            :class="['cloud-transfer', { 'cloud-transfer--completed': job.status === 'Completed' }]">
            <div v-if="job.status === 'Completed'" class="settings__item-label">
                #{{ job.recorded_video_id }} · {{ job.direction === 'ToCloud' ? 'クラウドへ移動' : 'ローカルへ移動' }} · 完了
            </div>
            <div v-else class="d-flex align-center flex-wrap ga-2">
                <strong>#{{ job.recorded_video_id }} · {{ job.direction === 'ToCloud' ? 'クラウドへ移動' : 'ローカルへ移動' }}</strong>
                <v-chip size="small" :color="job.status === 'Failed' ? 'error' : 'primary'" variant="tonal">
                    {{ stateLabel(job) }}
                </v-chip>
            </div>
            <p v-if="job.status === 'Running'" class="settings__item-label mt-2">{{ phaseDescriptions[job.phase] }}</p>
            <p v-if="job.status === 'Failed'" class="settings__item-label mt-2">
                {{ job.cancel_requested ? '取り消し処理に伴う未公開コピーの削除に失敗しました。「清掃を再開」から削除処理をやり直してください。' :
                    job.phase === 'Cleaning' ? '移動先への所在切替は完了していますが、移動元のファイル削除に失敗しました。' :
                    '移動処理の途中でエラーが発生し、処理を中断しました。' }}
            </p>
            <p v-if="job.status === 'Failed' && job.error_code === 'CryptKeyChanged'" class="settings__item-label mt-2">
                移動開始時と異なる暗号化鍵が設定されているため、処理を中断しました。設定画面で開始時の暗号化鍵（パスワードおよびソルト）の状態を確認し、同一の鍵を再登録した上で処理を再開してください。
            </p>
            <div class="d-flex flex-wrap ga-2 mt-3">
                <v-btn v-if="job.status === 'Failed'" variant="tonal" :disabled="busy" @click="retry(job)">
                    {{ job.cancel_requested ? '清掃を再開' : '処理を再開' }}
                </v-btn>
                <v-btn v-if="canCancel(job)" variant="text" :disabled="busy" @click="cancelTarget = job">処理を取り消す</v-btn>
            </div>
        </article>
        <v-dialog :model-value="cancelTarget !== null" max-width="600" :persistent="busy"
            @update:model-value="value => { if (!value) cancelTarget = null; }">
            <v-card>
                <v-card-title class="pt-6 px-6">移動処理の取り消し</v-card-title>
                <v-card-text>この移動処理を取り消します。公開前の処理のみ取り消し可能で、移動先に作成された未公開の一時データは削除されますが、元の録画ファイルは削除されません。</v-card-text>
                <v-card-actions class="px-6 pb-6">
                    <v-spacer />
                    <v-btn :disabled="busy" @click="cancelTarget = null">キャンセル</v-btn>
                    <v-btn color="error" :loading="busy" @click="cancel">取り消しを実行</v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>
    </div>
</template>

<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue';

import Message from '@/message';
import KonomiTVBS4KCloudTransfers, { IKonomiTVBS4KCloudCatalogSync, IKonomiTVBS4KCloudTransfer } from '@/services/KonomiTVBS4KCloudTransfers';

const jobs = ref<IKonomiTVBS4KCloudTransfer[]>([]);
const syncStates = ref<IKonomiTVBS4KCloudCatalogSync[]>([]);
const syncLoadFailed = ref(false);
const syncRequested = ref(false);
const loading = ref(false);
const loadFailed = ref(false);
const busy = ref(false);
const cancelTarget = ref<IKonomiTVBS4KCloudTransfer | null>(null);
let timer: ReturnType<typeof setTimeout> | null = null;
let disposed = false;
const statusLabels = { Pending: '待機中', Running: '処理中', Failed: '失敗', Completed: '完了', Cancelled: '取消済み' };
const phaseLabels = {
    Preparing: '準備中', Copying: 'コピー中', Verifying: '検証中', Publishing: '公開切替中',
    Cleaning: '後処理中', Completed: '完了', Cancelling: '取消清掃中', Cancelled: '取消済み',
};
const phaseDescriptions = {
    Preparing: '移動対象の録画ファイルと転送目録を確定しています。',
    Copying: '移動先へ対象ファイルをコピーしています（この段階では移動完了ではありません）。',
    Verifying: '移動先にコピーされたデータの内容と整合性を検証しています。',
    Publishing: '移動先での読取りを確認し、録画の所在情報を移動先へ切り替えています。',
    Cleaning: '所在切替が完了したため、移動元に残った元の録画ファイルを削除しています。',
    Cancelling: '取り消しに伴い、移動先に作成された未公開の一時コピーを削除しています。',
    Completed: '', Cancelled: '',
};

function stateLabel(job: IKonomiTVBS4KCloudTransfer): string {
    if (job.status === 'Failed' || job.status === 'Completed' || job.status === 'Cancelled') return statusLabels[job.status];
    if (job.cancel_requested && job.phase !== 'Cancelling') return '取消要求済み';
    return job.status === 'Pending' ? statusLabels.Pending : phaseLabels[job.phase];
}

function canCancel(job: IKonomiTVBS4KCloudTransfer): boolean {
    return !job.cancel_requested && ['Pending', 'Running', 'Failed'].includes(job.status) &&
        ['Preparing', 'Copying', 'Verifying'].includes(job.phase);
}

async function load() {
    if (loading.value || disposed) return;
    if (timer !== null) clearTimeout(timer);
    loading.value = true;
    try {
        const [result, syncResult] = await Promise.all([KonomiTVBS4KCloudTransfers.list(), KonomiTVBS4KCloudTransfers.syncStatus()]);
        if (disposed) return;
        loadFailed.value = result.type === 'error';
        if (result.type === 'success') jobs.value = result.data;
        syncLoadFailed.value = syncResult.type === 'error';
        if (syncResult.type === 'success') {
            syncStates.value = syncResult.data;
            if (syncRequested.value && syncStates.value.some(item => item.status === 'Failed')) {
                Message.error('クラウド目録の同期に失敗しました。通信切断等により最新の目録を取得できていない可能性があります。');
                syncRequested.value = false;
            } else if (syncRequested.value && syncStates.value.every(item => item.status === 'Completed')) {
                Message.success('クラウド目録の同期が完了しました。');
                syncRequested.value = false;
            }
        }
    } finally {
        loading.value = false;
        if (!disposed) timer = setTimeout(load, 5000);
    }
}

async function retry(job: IKonomiTVBS4KCloudTransfer) {
    busy.value = true;
    try {
        const result = await KonomiTVBS4KCloudTransfers.retry(job.id);
        if (result.type === 'error') {
            Message.error(job.cancel_requested ? '清掃の再開要求を受け付けることができませんでした。' : '移動処理の再開要求を受け付けることができませんでした。');
        } else {
            Message.success(job.cancel_requested ? '未公開コピーの清掃再開を受け付けました。' : '移動処理の再開を受け付けました。バックグラウンドで処理を継続します。');
        }
        await load();
    } finally { busy.value = false; }
}

async function sync() {
    busy.value = true;
    try {
        const result = await KonomiTVBS4KCloudTransfers.sync();
        if (result.type === 'error') {
            Message.error('目録の同期要求を受け付けることができませんでした。');
        } else {
            syncStates.value = result.data;
            syncRequested.value = true;
            Message.success('クラウド目録の同期を受け付けました。バックグラウンドで処理を開始します。');
        }
        await load();
    } finally { busy.value = false; }
}

async function cancel() {
    if (cancelTarget.value === null) return;
    busy.value = true;
    try {
        const result = await KonomiTVBS4KCloudTransfers.cancel(cancelTarget.value.id);
        if (result.type === 'error') {
            Message.error(result.status === 409 ? '移動先への公開処理が開始されたため、取り消しできませんでした。' : '移動処理を取り消すことができませんでした。');
        } else {
            Message.success(result.data.status === 'Cancelled' ? '移動処理を取り消しました。' : '移動処理の取り消しを受け付けました。未公開コピーの清掃を開始します。');
            cancelTarget.value = null;
        }
        await load();
    } finally { busy.value = false; }
}

onMounted(load);
onBeforeUnmount(() => { disposed = true; if (timer !== null) clearTimeout(timer); });
</script>

<style scoped lang="scss">
.cloud-transfer {
    padding: 16px 0;
    border-bottom: 1px solid var(--color-background-lighten-2);
}
.cloud-transfer--completed {
    padding: 6px 0;
}
</style>
