<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:image-stack-20-filled" width="25px" />
            <span class="ml-3">CMロゴ管理</span>
        </h2>
        <div class="settings__description">
            AviUtl v0.1 の単一ロゴ .lgd と拡張 v1 を読み込み、SID・NID・TSID の割り当てと適用期間を管理します。
        </div>
        <div class="logo-toolbar mt-5">
            <v-text-field color="primary" variant="outlined" density="compact" hide-details
                prepend-inner-icon="mdi-magnify" placeholder="SID・名前・ファイル名を検索" v-model="search_query" />
            <v-btn variant="flat" :disabled="capabilities.automatic_logo_generation === 'Unavailable'">
                <Icon icon="fluent:image-add-20-filled" class="mr-2" />録画から自動作成（準備中）
            </v-btn>
            <v-btn variant="flat" :loading="is_loading" @click="loadData(true)">
                <Icon icon="fluent:arrow-sync-20-filled" class="mr-2" />再スキャン
            </v-btn>
        </div>
        <div class="logo-manager mt-4" :class="{'logo-manager--loading': is_loading}">
            <section class="logo-pane logo-services">
                <h3>サービス</h3>
                <button v-for="service_id in service_ids" :key="String(service_id)" type="button"
                    :class="{'logo-services__item--active': selected_service_id === service_id}"
                    @click="selectService(service_id)">
                    <span>{{service_id === 'unassigned' ? 'SID 未割り当て' : `SID ${service_id}`}}</span>
                    <strong>{{stationNamesForService(service_id)}}</strong>
                    <small>{{logosForService(service_id).length}} ロゴ</small>
                </button>
            </section>
            <section class="logo-pane logo-cards">
                <h3>ロゴ</h3>
                <button v-for="logo in filtered_logos" :key="logo.id" type="button" class="logo-card"
                    :class="{'logo-card--active': selected_logo_id === logo.id, 'logo-card--missing': logo.missing}"
                    @click="selected_logo_id = logo.id">
                    <div class="logo-card__preview">
                        <img v-if="!logo.missing" :src="logoPreviewURL(logo)" :alt="`${logo.logo_name} のプレビュー`">
                        <Icon v-else icon="fluent:image-off-20-regular" width="34px" />
                    </div>
                    <div class="logo-card__body">
                        <strong>{{logo.logo_name || logo.filename}}</strong>
                        <span>{{logo.filename}}</span>
                        <small>{{logo.service_id === null ? 'SID 未割り当て' : `SID ${logo.service_id}`}} · {{formatSize(logo.file_size)}}</small>
                        <small v-if="logo.missing" class="text-error-readable">missing / 外部削除済み</small>
                        <small v-else-if="!logo.enabled">無効</small>
                    </div>
                </button>
                <div v-if="filtered_logos.length === 0" class="logo-empty">該当するロゴはありません。</div>
            </section>
            <section class="logo-pane logo-detail">
                <template v-if="selected_logo !== null">
                    <h3>選択ロゴ詳細</h3>
                    <dl>
                        <dt>局名</dt><dd>{{selected_logo.logo_name}}</dd>
                        <dt>SID</dt><dd>{{selected_logo.service_id ?? '未割り当て（明示割り当てが必要）'}}</dd>
                        <dt>形式</dt><dd>{{formatLogoFileFormat(selected_logo.file_format)}}</dd>
                        <dt>ファイル</dt><dd>{{selected_logo.filename}}</dd>
                        <dt>パス</dt><dd>{{selected_logo.path}}</dd>
                        <dt>SHA-256</dt><dd class="logo-detail__hash">{{selected_logo.file_hash}}</dd>
                        <dt>最終利用</dt><dd>{{selected_logo.last_used_at ?? '未使用'}}</dd>
                        <dt>生成元録画</dt><dd>{{selected_logo.generated_from_recorded_video_id ?? '外部/手動追加'}}</dd>
                    </dl>
                    <v-switch color="primary" hide-details label="KonomiTV で有効"
                        :model-value="selected_logo.enabled" :disabled="selected_logo.missing"
                        @update:modelValue="updateSelectedLogo(Boolean($event))" />
                    <v-divider class="my-5"></v-divider>
                    <h4>NID・TSID サービス割り当て</h4>
                    <div v-for="assignment in selected_logo_assignments" :key="assignment.id" class="assignment-row">
                        <span>SID {{assignment.service_id}} / NID {{assignment.network_id}} / TSID {{assignment.transport_stream_id}}
                            <template v-if="assignment.valid_from || assignment.valid_until"><br>{{assignment.valid_from ?? '開始なし'}} ～ {{assignment.valid_until ?? '終了なし'}}</template>
                        </span>
                        <v-btn icon="mdi-delete" size="small" variant="text" @click="deleteAssignment(assignment.id)" />
                    </div>
                    <div class="assignment-form mt-3">
                        <v-text-field label="SID" type="number" density="compact" variant="outlined" hide-details v-model.number="assignment_service_id" />
                        <v-text-field label="NID" type="number" density="compact" variant="outlined" hide-details v-model.number="assignment_network_id" />
                        <v-text-field label="TSID" type="number" density="compact" variant="outlined" hide-details v-model.number="assignment_transport_stream_id" />
                        <v-text-field label="適用開始" type="datetime-local" density="compact" variant="outlined" hide-details v-model="assignment_valid_from" />
                        <v-text-field label="適用終了" type="datetime-local" density="compact" variant="outlined" hide-details v-model="assignment_valid_until" />
                        <v-btn variant="flat" @click="createLogoAssignment(false)">このロゴを割り当て</v-btn>
                        <v-btn variant="flat" color="background-lighten-2" @click="createLogoAssignment(true)">「ロゴなし」を割り当て</v-btn>
                    </div>
                    <v-btn v-if="!selected_logo.missing" class="mt-6" color="error" variant="flat" @click="deleteSelectedLogo()">
                        共有 .lgd を削除
                    </v-btn>
                    <div class="settings__item-label mt-2 text-error-readable">
                        共有ファイルを削除すると、同じフォルダを参照する外部ツールからも利用できなくなります。履歴は missing として残ります。
                    </div>
                </template>
                <template v-else>
                    <h3>ロゴ追加</h3>
                    <v-file-input accept=".lgd" label="単一ロゴ .lgd（AviUtl v0.1 / 拡張 v1）" variant="outlined" hide-details
                        @update:modelValue="uploadLogo($event)" />
                    <div class="settings__item-label mt-3">標準形式は追加後にSIDを明示割り当てします。.lgd2 と複数ロゴは非対応です。</div>
                </template>
            </section>
        </div>
    </SettingsBase>
</template>

<script setup lang="ts">

import { computed, onMounted, ref, watch } from 'vue';

import Message from '@/message';
import CMAnalysis, { ICMAnalysisCapabilities, ICMLogo, ICMLogoServiceAssignment } from '@/services/CMAnalysis';
import SettingsBase from '@/views/Settings/Base.vue';


const logos = ref<ICMLogo[]>([]);
const assignments = ref<ICMLogoServiceAssignment[]>([]);
const capabilities = ref<ICMAnalysisCapabilities>({automatic_logo_generation: 'Unavailable'});
const search_query = ref('');
type ServiceKey = number | 'unassigned';
const selected_service_id = ref<ServiceKey | null>(null);
const selected_logo_id = ref<number | null>(null);
const is_loading = ref(false);
const assignment_service_id = ref<number | null>(null);
const assignment_network_id = ref<number | null>(null);
const assignment_transport_stream_id = ref<number | null>(null);
const assignment_valid_from = ref('');
const assignment_valid_until = ref('');

const service_ids = computed<ServiceKey[]>(() => {
    const assigned = [...new Set(logos.value
        .map((logo) => logo.service_id)
        .filter((service_id): service_id is number => service_id !== null))].sort((a, b) => a - b);
    return logos.value.some((logo) => logo.service_id === null) ? [...assigned, 'unassigned'] : assigned;
});
const filtered_logos = computed(() => {
    const query = search_query.value.trim().toLowerCase();
    return logos.value.filter((logo) => {
        const service_matches = selected_service_id.value === null ||
            (selected_service_id.value === 'unassigned' ? logo.service_id === null : logo.service_id === selected_service_id.value);
        const query_matches = query === '' || `${logo.service_id ?? '未割り当て'} ${logo.logo_name} ${logo.filename}`.toLowerCase().includes(query);
        return service_matches && query_matches;
    });
});
const selected_logo = computed(() => logos.value.find((logo) => logo.id === selected_logo_id.value) ?? null);
const selected_logo_assignments = computed(() => assignments.value.filter((assignment) => assignment.logo_id === selected_logo_id.value));

watch(selected_logo, (logo) => {
    assignment_service_id.value = logo?.service_id ?? null;
});

function logoPreviewURL(logo: ICMLogo): string {
    return `/api/cm-analysis/logos/${logo.id}/preview?v=${logo.file_hash}`;
}

onMounted(() => loadData(false));

async function loadData(rescan: boolean): Promise<void> {
    is_loading.value = true;
    const [new_logos, new_assignments, new_capabilities] = await Promise.all([
        CMAnalysis.fetchLogos(rescan),
        CMAnalysis.fetchAssignments(),
        CMAnalysis.fetchCapabilities(),
    ]);
    if (new_logos !== null) logos.value = new_logos;
    if (new_assignments !== null) assignments.value = new_assignments;
    if (new_capabilities !== null) capabilities.value = new_capabilities;
    if (selected_service_id.value === null && service_ids.value.length > 0) selected_service_id.value = service_ids.value[0];
    is_loading.value = false;
}

function logosForService(service_id: ServiceKey): ICMLogo[] {
    return logos.value.filter((logo) => service_id === 'unassigned' ? logo.service_id === null : logo.service_id === service_id);
}

function stationNamesForService(service_id: ServiceKey): string {
    const station_names = logosForService(service_id)
        .map((logo) => logo.logo_name.replace(/\(\d{4}-\d{2}-\d{2}\)$/, '').trim())
        .filter((name) => name !== '' && name !== 'No Name');
    const unique_station_names = [...new Set(station_names)];
    return unique_station_names.length > 0 ? unique_station_names.join(' / ') : '局名不明';
}

function selectService(service_id: ServiceKey): void {
    selected_service_id.value = service_id;
    selected_logo_id.value = null;
}

function formatSize(size: number): string {
    return size < 1024 ? `${size} B` : `${(size / 1024).toFixed(1)} KiB`;
}

function formatLogoFileFormat(file_format: ICMLogo['file_format']): string {
    return file_format === 'AviUtlV0.1' ? 'AviUtl v0.1（標準単一ロゴ）' : '拡張 v1';
}

async function updateSelectedLogo(enabled: boolean): Promise<void> {
    if (selected_logo.value === null) return;
    if (await CMAnalysis.updateLogo(selected_logo.value.id, enabled)) {
        selected_logo.value.enabled = enabled;
    }
}

async function uploadLogo(value: File | File[] | null): Promise<void> {
    const file = Array.isArray(value) ? value[0] : value;
    if (file === null || file === undefined) return;
    const logo = await CMAnalysis.uploadLogo(file);
    if (logo !== null) {
        Message.success('共有 CM ロゴを追加しました。');
        await loadData(false);
        selected_service_id.value = logo.service_id ?? 'unassigned';
        selected_logo_id.value = logo.id;
    }
}

async function createLogoAssignment(no_logo: boolean): Promise<void> {
    if (
        selected_logo.value === null || assignment_service_id.value === null ||
        assignment_network_id.value === null || assignment_transport_stream_id.value === null
    ) {
        Message.warning('SID、NID、TSID を入力してください。');
        return;
    }
    const result = await CMAnalysis.createAssignment({
        logo_id: no_logo ? null : selected_logo.value.id,
        network_id: assignment_network_id.value,
        transport_stream_id: assignment_transport_stream_id.value,
        service_id: assignment_service_id.value,
        enabled: true,
        is_no_logo: no_logo,
        valid_from: assignment_valid_from.value ? new Date(assignment_valid_from.value).toISOString() : null,
        valid_until: assignment_valid_until.value ? new Date(assignment_valid_until.value).toISOString() : null,
    });
    if (result !== null) {
        assignments.value.push(result);
        Message.success(no_logo ? '「ロゴなし」を割り当てました。' : 'ロゴをサービスへ割り当てました。');
    }
}

async function deleteAssignment(assignment_id: number): Promise<void> {
    if (await CMAnalysis.deleteAssignment(assignment_id)) {
        assignments.value = assignments.value.filter((assignment) => assignment.id !== assignment_id);
    }
}

async function deleteSelectedLogo(): Promise<void> {
    if (selected_logo.value === null) return;
    if (!window.confirm('共有 .lgd を削除します。同じフォルダを参照する外部ツールにも影響します。続行しますか？')) return;
    if (await CMAnalysis.deleteLogo(selected_logo.value.id)) {
        Message.success('共有ロゴを削除し、missing 履歴として保存しました。');
        selected_logo_id.value = null;
        await loadData(false);
    }
}

</script>

<style lang="scss" scoped>

.logo-toolbar { display: grid; grid-template-columns: 1fr auto auto; gap: 12px; }
.logo-manager { display: grid; grid-template-columns: 150px minmax(220px, 1fr) minmax(300px, 1.2fr); gap: 12px; min-height: 520px; }
.logo-manager--loading { opacity: 0.65; pointer-events: none; }
.logo-pane { padding: 14px; border-radius: 10px; background: rgb(var(--v-theme-background-lighten-2)); overflow: auto; }
.logo-pane h3 { margin-bottom: 12px; font-size: 17px; }
.logo-services button { display: flex; flex-direction: column; width: 100%; padding: 10px; margin-bottom: 6px; border-radius: 8px; text-align: left; }
.logo-services button:hover, .logo-services__item--active { background: rgb(var(--v-theme-primary) / 18%); }
.logo-services strong { width: 100%; margin: 2px 0; font-size: 12px; line-height: 1.45; overflow-wrap: anywhere; white-space: normal; }
.logo-services small, .logo-card small { color: rgb(var(--v-theme-text-darken-1)); }
.logo-card { display: grid; grid-template-columns: 64px 1fr; width: 100%; padding: 9px; margin-bottom: 8px; border: 2px solid transparent; border-radius: 9px; text-align: left; background: rgb(var(--v-theme-background-lighten-1)); }
.logo-card--active { border-color: rgb(var(--v-theme-primary)); }
.logo-card--missing { opacity: 0.6; }
.logo-card__preview { display: flex; align-items: center; justify-content: center; }
.logo-card__preview img { max-width: 58px; max-height: 42px; object-fit: contain; }
.logo-card__body { display: flex; min-width: 0; flex-direction: column; }
.logo-card__body strong { overflow-wrap: anywhere; }
.logo-card__body span, .logo-card__body small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.logo-empty { padding: 24px 8px; text-align: center; opacity: 0.7; }
.logo-detail dl { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 7px 10px; margin-bottom: 16px; font-size: 13px; }
.logo-detail dt { font-weight: 700; }
.logo-detail dd { overflow-wrap: anywhere; }
.logo-detail__hash { font-family: monospace; }
.assignment-row { display: flex; align-items: center; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid rgb(var(--v-theme-background-lighten-1)); font-size: 12px; }
.assignment-form { display: grid; gap: 8px; }

@include tablet-vertical {
    .logo-manager { grid-template-columns: 120px 1fr; }
    .logo-detail { grid-column: 1 / -1; }
}
@include smartphone-horizontal {
    .logo-manager { grid-template-columns: 120px 1fr; }
    .logo-detail { grid-column: 1 / -1; }
}
@include smartphone-vertical {
    .logo-toolbar { grid-template-columns: 1fr; }
    .logo-manager { grid-template-columns: 1fr; }
    .logo-services { display: flex; gap: 6px; }
    .logo-services h3 { display: none; }
    .logo-services button { min-width: 0; flex: 0 0 clamp(150px, 42vw, 220px); }
}

</style>
