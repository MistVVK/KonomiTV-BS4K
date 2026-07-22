<template>
    <v-dialog :model-value="modelValue" :fullscreen="Utils.isSmartphoneVertical()" :persistent="is_saving"
        max-width="620" scrollable @update:model-value="updateDialog">
        <v-card class="recorded-episode-dialog">
            <v-card-title class="recorded-episode-dialog__title">話数を訂正</v-card-title>

            <v-card-text v-if="is_loading" class="recorded-episode-dialog__state">
                <v-progress-circular color="primary" indeterminate size="32" width="3" />
                <span>話数情報を取得しています…</span>
            </v-card-text>
            <v-card-text v-else-if="load_failed" class="recorded-episode-dialog__state">
                <Icon icon="fluent:error-circle-20-regular" width="29px" />
                <span>話数情報を取得できませんでした。</span>
                <v-btn color="primary" size="small" variant="tonal" @click="loadAssignments()">再試行</v-btn>
            </v-card-text>
            <v-card-text v-else-if="target_program === null" class="recorded-episode-dialog__state">
                <Icon icon="fluent:info-20-regular" width="29px" />
                <span>この録画は、選択したシリーズに所属していません。</span>
            </v-card-text>
            <v-card-text v-else>
                <div class="recorded-episode-dialog__program">
                    <strong>{{target_program.title}}</strong>
                    <span v-if="target_program.subtitle !== null">{{target_program.subtitle}}</span>
                    <small>
                        {{dayjs(target_program.start_time).format('YYYY/M/D (ddd) HH:mm')}}
                        ・{{target_program.channel_name ?? 'チャンネル情報なし'}}
                    </small>
                </div>

                <div class="recorded-episode-dialog__current mt-3">
                    <span>現在の構造化話数</span>
                    <strong>{{current_episode_label}}</strong>
                    <small v-if="target_program.legacy_episode_number !== null">
                        元の番組情報: {{target_program.legacy_episode_number}}
                    </small>
                </div>

                <div v-if="target_program.resolution !== null" class="recorded-episode-dialog__resolution mt-3">
                    <div>
                        <span>判定状態</span>
                        <strong>{{resolutionStatusLabel(target_program.resolution.status)}}</strong>
                    </div>
                    <div v-if="target_program.resolution.source !== null">
                        <span>判定元</span>
                        <strong>{{resolutionSourceLabel(target_program.resolution.source)}}</strong>
                    </div>
                    <div v-if="target_program.resolution.confidence !== null">
                        <span>信頼度</span>
                        <strong>{{Math.round(target_program.resolution.confidence * 100)}}%</strong>
                    </div>
                    <div v-if="target_program.resolution.citations.length > 0">
                        <span>Web 出典</span>
                        <strong>{{target_program.resolution.citations.length.toLocaleString()}} 件</strong>
                    </div>
                </div>
                <div v-if="target_program.resolution?.citations.length" class="recorded-episode-dialog__citations mt-2">
                    <span>判定に使った Web 出典</span>
                    <a v-for="citation in target_program.resolution.citations" :key="citation.url"
                        :href="citation.url" target="_blank" rel="noopener noreferrer">
                        <Icon icon="fluent:open-20-regular" width="15px" />
                        {{citation.title || citation.url}}
                    </a>
                </div>

                <v-radio-group class="mt-3" v-model="assignment_mode" color="primary" hide-details>
                    <v-radio label="登録済みの話数を選ぶ" value="Existing" />
                    <v-radio label="シーズン・話数を入力する" value="Structured" />
                    <v-radio label="話数不明として確定する" value="Unknown" />
                </v-radio-group>

                <v-select v-if="assignment_mode === 'Existing'" class="mt-3"
                    v-model="selected_episode_id" :items="episode_options" item-title="title" item-value="value"
                    label="登録済みの話数" color="primary" variant="outlined" density="comfortable"
                    no-data-text="登録済みの話数はありません" hide-details="auto" />

                <div v-else-if="assignment_mode === 'Structured'" class="recorded-episode-dialog__structured mt-3">
                    <v-text-field v-model.number="season_number" type="number" :min="0" :max="2147483647" :step="1"
                        label="シーズン" color="primary" variant="outlined" density="comfortable"
                        :error-messages="season_number_error" hide-details="auto" />
                    <v-text-field v-model="episode_number" type="text" inputmode="decimal"
                        label="話数" placeholder="例: 12 または 12.5" color="primary" variant="outlined"
                        density="comfortable" maxlength="11" :error-messages="episode_number_error"
                        hide-details="auto" />
                </div>

                <div v-else class="recorded-episode-dialog__notice mt-3">
                    自動判定で再び上書きされないよう、この録画は管理者が「話数不明」と判断した結果として保存します。
                </div>

                <div class="recorded-episode-dialog__note mt-3">
                    <Icon icon="fluent:info-20-regular" width="18px" />
                    <span>同じシーズン・話数を指定した複数局の録画は、同一話としてまとめたまま個別の録画として残ります。</span>
                </div>
            </v-card-text>

            <v-card-actions>
                <v-spacer />
                <v-btn variant="text" :disabled="is_saving" @click="updateDialog(false)">キャンセル</v-btn>
                <v-btn v-if="target_program !== null" color="primary" variant="flat" :loading="is_saving"
                    :disabled="submit_disabled" @click="saveAssignment()">
                    保存
                </v-btn>
            </v-card-actions>
        </v-card>
    </v-dialog>
</template>

<script setup lang="ts">

import { computed, ref, watch } from 'vue';

import Message from '@/message';
import RecordedSeries, {
    type IRecordedEpisodeAssignmentList,
    type IRecordedEpisodeAssignmentProgram,
    type IRecordedEpisodeAssignmentResolution,
    type IRecordedEpisodeAssignmentUpdate,
} from '@/services/RecordedSeries';
import Utils, { dayjs } from '@/utils';
import { formatRecordedEpisodeNumber } from '@/utils/RecordedEpisode';


type AssignmentMode = 'Existing' | 'Structured' | 'Unknown';

const props = defineProps<{
    modelValue: boolean;
    seriesId: number;
    recordedProgramId: number | null;
    assignmentList?: IRecordedEpisodeAssignmentList | null;
}>();
const emit = defineEmits<{
    (e: 'update:modelValue', value: boolean): void;
    (e: 'saved', value: IRecordedEpisodeAssignmentList | null): void;
}>();

const assignments = ref<IRecordedEpisodeAssignmentList | null>(null);
const is_loading = ref(false);
const load_failed = ref(false);
const is_saving = ref(false);
const assignment_mode = ref<AssignmentMode>('Structured');
const selected_episode_id = ref<number | null>(null);
const season_number = ref<number | string>(1);
const episode_number = ref('');
let request_sequence = 0;

const target_program = computed<IRecordedEpisodeAssignmentProgram | null>(() => {
    if (props.recordedProgramId === null) return null;
    return assignments.value?.programs.find(program => program.recorded_program_id === props.recordedProgramId) ?? null;
});
const current_episode = computed(() => {
    if (target_program.value?.series_episode_id === null || target_program.value === null) return null;
    return assignments.value?.episodes.find(episode => episode.id === target_program.value?.series_episode_id) ?? null;
});
const episode_options = computed(() => assignments.value?.episodes.map(episode => ({
    title: formatEpisodeLabel(episode.season_number, episode.episode_number),
    value: episode.id,
})) ?? []);
const current_episode_label = computed(() => current_episode.value === null ?
    '未設定' :
    formatEpisodeLabel(current_episode.value.season_number, current_episode.value.episode_number));
const season_number_error = computed(() => {
    const raw_value = String(season_number.value).trim();
    if (raw_value === '') return '0 ～ 2147483647 の整数を入力してください。';
    const value = Number(raw_value);
    return Number.isSafeInteger(value) && value >= 0 && value <= 2_147_483_647 ?
        '' :
        '0 ～ 2147483647 の整数を入力してください。';
});
const episode_number_error = computed(() => {
    const value = episode_number.value.trim();
    if (value === '') return '話数を入力してください。';
    if (/^\d{1,7}(?:\.\d{1,3})?$/.test(value) === false) {
        return '整数部 7 桁、小数部 3 桁以内の 0 以上の数値を入力してください。';
    }
    return '';
});
const submit_disabled = computed(() => {
    if (is_saving.value || target_program.value === null) return true;
    if (assignment_mode.value === 'Existing') return selected_episode_id.value === null;
    if (assignment_mode.value === 'Structured') {
        return season_number_error.value !== '' || episode_number_error.value !== '';
    }
    return false;
});


function formatEpisodeLabel(season: number, episode: string): string {
    return `シーズン ${season}・第 ${formatRecordedEpisodeNumber(episode)} 話`;
}

function resolutionStatusLabel(status: IRecordedEpisodeAssignmentResolution['status']): string {
    return {
        Pending: '判定待ち',
        Resolved: '確定',
        Unknown: '話数不明',
        NotNumbered: '公式話数なし',
        NeedsReview: '要確認',
        Failed: '判定失敗',
    }[status];
}

function resolutionSourceLabel(source: NonNullable<IRecordedEpisodeAssignmentResolution['source']>): string {
    return {
        Local: 'ローカル情報',
        EPG: 'EPG',
        WebSearch: 'AI Web 検索',
        Manual: '管理者の手動訂正',
        Migration: '既存データ移行',
    }[source];
}

/** 読み込んだ現在値または AI の未確定提案を、編集フォームの初期値に反映する。 */
function initializeDraft(): void {
    const program = target_program.value;
    if (program === null) return;

    if (program.series_episode_id !== null) {
        assignment_mode.value = 'Existing';
        selected_episode_id.value = program.series_episode_id;
        const episode = current_episode.value;
        season_number.value = episode?.season_number ?? 1;
        episode_number.value = episode?.episode_number ?? '';
        return;
    }
    if (
        program.resolution?.proposed_season_number !== null &&
        program.resolution?.proposed_season_number !== undefined &&
        program.resolution.proposed_episode_number !== null
    ) {
        assignment_mode.value = 'Structured';
        season_number.value = program.resolution.proposed_season_number;
        episode_number.value = program.resolution.proposed_episode_number;
        selected_episode_id.value = null;
        return;
    }
    if (program.resolution?.source === 'Manual' && program.resolution.status === 'Unknown') {
        assignment_mode.value = 'Unknown';
    } else {
        assignment_mode.value = 'Structured';
    }
    selected_episode_id.value = null;
    season_number.value = 1;
    episode_number.value = '';
}

/** 呼び出し元の値を初期表示へ使いつつ、開くたびに最新の割当を API から取得する。 */
async function loadAssignments(): Promise<void> {
    const sequence = ++request_sequence;
    load_failed.value = false;
    const provided_assignments_are_usable = (
        props.assignmentList?.series_id === props.seriesId &&
        props.assignmentList.programs.some(program => program.recorded_program_id === props.recordedProgramId)
    );
    if (provided_assignments_are_usable && props.assignmentList !== undefined) {
        assignments.value = props.assignmentList;
        initializeDraft();
    }

    is_loading.value = true;
    const result = await RecordedSeries.fetchEpisodeAssignments(props.seriesId);
    if (sequence !== request_sequence) return;
    is_loading.value = false;
    assignments.value = result;
    load_failed.value = result === null;
    if (result !== null) initializeDraft();
}

function updateDialog(value: boolean): void {
    if (is_saving.value) return;
    emit('update:modelValue', value);
}

/** 楽観ロック値を含めて保存し、成功後は候補と録画割当をまとめて再取得する。 */
async function saveAssignment(): Promise<void> {
    const program = target_program.value;
    if (program === null || submit_disabled.value) return;

    const common = {
        expected_series_id: props.seriesId,
        expected_series_episode_id: program.series_episode_id,
    };
    let update: IRecordedEpisodeAssignmentUpdate;
    if (assignment_mode.value === 'Existing') {
        if (selected_episode_id.value === null) return;
        update = {...common, decision: 'ExistingEpisode', episode_id: selected_episode_id.value};
    } else if (assignment_mode.value === 'Structured') {
        update = {
            ...common,
            decision: 'StructuredEpisode',
            season_number: Number(season_number.value),
            episode_number: episode_number.value.trim(),
        };
    } else {
        update = {...common, decision: 'Unknown'};
    }

    is_saving.value = true;
    const result = await RecordedSeries.updateEpisodeAssignment(program.recorded_program_id, update);
    if (result.type === 'Stale') {
        Message.error('別の画面でシリーズまたは話数が変更されました。最新情報を読み込み直しました。');
        is_saving.value = false;
        await loadAssignments();
        return;
    }
    if (result.type === 'NotFound') {
        Message.error('録画または選択した話数が見つかりません。最新情報を読み込み直してください。');
        is_saving.value = false;
        await loadAssignments();
        return;
    }
    if (result.type === 'Error') {
        is_saving.value = false;
        return;
    }

    // PUT は 204 のため、同じ API から新しい Episode ID と表示状態を取得する。
    const refreshed = await RecordedSeries.fetchEpisodeAssignments(props.seriesId, false);
    assignments.value = refreshed;
    is_saving.value = false;
    emit('saved', refreshed);
    emit('update:modelValue', false);
    Message.success(assignment_mode.value === 'Unknown' ? '話数不明として保存しました。' : '録画の話数を更新しました。');
}

watch(() => props.modelValue, (is_open) => {
    if (is_open) {
        void loadAssignments();
    } else {
        request_sequence += 1;
        is_loading.value = false;
        load_failed.value = false;
    }
});

</script>

<style lang="scss" scoped>

.recorded-episode-dialog {
    color: rgb(var(--v-theme-text));
    background: rgb(var(--v-theme-background-lighten-1));

    &__title {
        font-weight: bold;
    }

    &__state {
        display: flex;
        align-items: center;
        justify-content: center;
        flex-direction: column;
        min-height: 210px;
        gap: 12px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 13px;
        text-align: center;
    }

    &__program,
    &__current {
        display: flex;
        flex-direction: column;
        padding: 11px 13px;
        border-radius: 7px;
        background: rgb(var(--v-theme-background));

        strong {
            font-size: 14px;
            overflow-wrap: anywhere;
        }

        span,
        small {
            margin-top: 3px;
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 11.5px;
            line-height: 1.5;
            overflow-wrap: anywhere;
        }
    }

    &__current {
        span {
            margin: 0;
        }

        strong {
            margin-top: 2px;
        }
    }

    &__resolution {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 7px;

        > div {
            display: flex;
            flex-direction: column;
            padding: 8px 10px;
            border-radius: 6px;
            background: rgba(var(--v-theme-primary), 0.09);

            span {
                color: rgb(var(--v-theme-text-darken-1));
                font-size: 10.5px;
            }

            strong {
                margin-top: 2px;
                font-size: 12.5px;
            }
        }
    }

    &__citations {
        display: flex;
        flex-direction: column;
        padding: 9px 11px;
        gap: 5px;
        border-radius: 6px;
        background: rgb(var(--v-theme-background));

        > span {
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 10.5px;
        }

        > a {
            display: flex;
            align-items: center;
            min-width: 0;
            gap: 5px;
            color: rgb(var(--v-theme-primary));
            font-size: 11.5px;
            overflow-wrap: anywhere;

            > svg {
                flex-shrink: 0;
            }
        }
    }

    &__structured {
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
        gap: 10px;
    }

    &__notice,
    &__note {
        padding: 10px 12px;
        border-radius: 6px;
        color: rgb(var(--v-theme-text-darken-1));
        background: rgb(var(--v-theme-background));
        font-size: 11.5px;
        line-height: 1.6;
    }

    &__note {
        display: flex;
        align-items: flex-start;
        gap: 7px;
        background: rgba(var(--v-theme-primary), 0.09);

        svg {
            flex-shrink: 0;
            margin-top: 1px;
            color: rgb(var(--v-theme-primary-readable));
        }
    }
}

@include smartphone-vertical {
    .recorded-episode-dialog {
        &__resolution,
        &__structured {
            grid-template-columns: minmax(0, 1fr);
        }
    }
}

</style>
