<template>
    <v-dialog :model-value="modelValue" :fullscreen="Utils.isSmartphoneVertical()" max-width="560"
        :persistent="is_assigning" @update:model-value="updateDialog">
        <v-card class="series-assignment-dialog">
            <v-card-title class="series-assignment-dialog__title">シリーズを訂正</v-card-title>
            <v-card-text>
                <div class="series-assignment-dialog__current">
                    <span>現在の分類</span>
                    <strong>{{currentLabel}}</strong>
                </div>
                <div v-if="programTitle !== null" class="series-assignment-dialog__program">
                    <span>対象録画</span>
                    <strong>{{programTitle}}</strong>
                </div>
                <div class="series-assignment-dialog__rule-note">
                    <Icon icon="fluent:info-20-regular" width="18px" />
                    <span>この訂正は、同じ番組名として判定される今後の録画にも適用されます。</span>
                </div>

                <v-radio-group v-model="assignment_mode" color="primary" hide-details>
                    <v-radio label="既存シリーズに割り当て" value="Existing" />
                    <v-radio label="新しいシリーズ名で割り当て" value="New" />
                    <v-radio label="単発番組に変更" value="NotSeries" />
                </v-radio-group>

                <v-autocomplete v-if="assignment_mode === 'Existing'" class="mt-3"
                    v-model="assignment_series_id" v-model:search="assignment_search_query"
                    :items="assignment_search_results" :loading="is_searching_series"
                    item-title="title" item-value="id" label="シリーズを検索" placeholder="シリーズ名を入力"
                    color="primary" variant="outlined" density="comfortable" clearable no-filter
                    hide-details="auto" no-data-text="一致するシリーズがありません"
                    @update:search="queueSeriesSearch" />

                <template v-else-if="assignment_mode === 'New'">
                    <v-text-field class="mt-3" v-model="new_series_title" label="新しいシリーズ名"
                        placeholder="シリーズ名を入力" color="primary" variant="outlined" density="comfortable"
                        maxlength="255" counter hide-details="auto" />
                    <div class="series-assignment-dialog__hint">
                        同じシリーズ名がすでに存在する場合は、新規作成せず既存シリーズへ割り当てます。
                    </div>
                </template>

                <div v-else class="series-assignment-dialog__notice">
                    この録画のシリーズ割り当てを外し、単発番組として扱います。
                </div>
            </v-card-text>
            <v-card-actions>
                <v-spacer />
                <v-btn variant="text" :disabled="is_assigning" @click="updateDialog(false)">
                    キャンセル
                </v-btn>
                <v-btn color="primary" variant="flat" :loading="is_assigning"
                    :disabled="assignment_submit_disabled" @click="submitAssignment()">
                    変更
                </v-btn>
            </v-card-actions>
        </v-card>
    </v-dialog>
</template>

<script setup lang="ts">

import { computed, ref, watch } from 'vue';

import Message from '@/message';
import RecordedSeries, { type IRecordedSeriesAssignment } from '@/services/RecordedSeries';
import Series, { type ISeries } from '@/services/Series';
import Utils from '@/utils';


type AssignmentMode = 'Existing' | 'New' | 'NotSeries';

const props = defineProps<{
    modelValue: boolean;
    recordedProgramId: number | null;
    currentSeriesId: number | null;
    currentSeriesTitle: string | null;
    currentLabel: string;
    programTitle?: string | null;
}>();

const emit = defineEmits<{
    (e: 'update:modelValue', value: boolean): void;
    (e: 'saved', payload: {
        recorded_program_id: number;
        decision: IRecordedSeriesAssignment['decision'];
        series_id: number | null;
    }): void;
}>();

const assignment_mode = ref<AssignmentMode>('Existing');
const assignment_series_id = ref<number | null>(null);
const assignment_search_query = ref('');
const assignment_search_results = ref<ISeries[]>([]);
const new_series_title = ref('');
const is_searching_series = ref(false);
const is_assigning = ref(false);
let series_search_timer: number | null = null;
let series_search_sequence = 0;

const assignment_submit_disabled = computed(() => {
    if (is_assigning.value || props.recordedProgramId === null) return true;
    if (assignment_mode.value === 'Existing') return assignment_series_id.value === null;
    if (assignment_mode.value === 'New') {
        const title = new_series_title.value.trim();
        return title.length === 0 || title.length > 255;
    }
    return false;
});

function updateDialog(value: boolean): void {
    if (value === false && is_assigning.value) return;
    emit('update:modelValue', value);
}

/** ダイアログを開いた時点の所属を初期値にし、候補検索を開始する。 */
function initializeDialog(): void {
    assignment_mode.value = props.currentSeriesId === null ? 'New' : 'Existing';
    assignment_series_id.value = props.currentSeriesId;
    assignment_search_query.value = '';
    assignment_search_results.value = [];
    new_series_title.value = props.currentSeriesTitle ?? props.programTitle ?? '';
    void searchSeries('');
}

/** 入力中の検索を短時間まとめ、古い検索結果が新しい検索語を上書きしないようにする。 */
function queueSeriesSearch(query: string | null): void {
    if (series_search_timer !== null) window.clearTimeout(series_search_timer);
    series_search_timer = window.setTimeout(() => {
        series_search_timer = null;
        void searchSeries(query?.trim() ?? '');
    }, 300);
}

/** 空文字では最近更新されたシリーズ、入力時は一致する既存シリーズを取得する。 */
async function searchSeries(query: string): Promise<void> {
    const request_sequence = ++series_search_sequence;
    is_searching_series.value = true;
    const result = query === '' ?
        await Series.fetchSeriesList('desc', 1) :
        await Series.searchSeries(query, 'desc', 1);
    if (request_sequence !== series_search_sequence) return;

    is_searching_series.value = false;
    if (result === null) return;

    // 現在のシリーズが検索上位から外れても、初期選択だけは失わない。
    const series_list = [...result.series_list];
    if (
        props.currentSeriesId !== null &&
        series_list.some(series => series.id === props.currentSeriesId) === false
    ) {
        series_list.unshift({
            id: props.currentSeriesId,
            title: props.currentSeriesTitle ?? `シリーズ #${props.currentSeriesId}`,
            description: '',
            genres: [],
            broadcast_periods: [],
            created_at: '',
            updated_at: '',
        });
    }
    assignment_search_results.value = series_list;
}

/** 選択した訂正を保存し、呼び出し側へ更新後の所属を通知する。 */
async function submitAssignment(): Promise<void> {
    if (assignment_submit_disabled.value || props.recordedProgramId === null) return;

    let assignment: IRecordedSeriesAssignment;
    if (assignment_mode.value === 'Existing') {
        if (assignment_series_id.value === null) return;
        assignment = {decision: 'Series', series_id: assignment_series_id.value};
    } else if (assignment_mode.value === 'New') {
        assignment = {decision: 'Series', series_title: new_series_title.value.trim()};
    } else {
        assignment = {decision: 'NotSeries'};
    }

    const program_id = props.recordedProgramId;
    is_assigning.value = true;
    const succeeded = await RecordedSeries.updateProgramAssignment(program_id, assignment);
    is_assigning.value = false;
    if (succeeded === false) return;

    emit('update:modelValue', false);
    emit('saved', {
        recorded_program_id: program_id,
        decision: assignment.decision,
        series_id: assignment.decision === 'Series' && 'series_id' in assignment ?
            assignment.series_id :
            null,
    });
    Message.success(assignment.decision === 'NotSeries' ?
        '単発番組へ変更しました。' :
        '録画番組のシリーズを変更しました。');
}

watch(() => props.modelValue, (is_open) => {
    if (is_open === false) {
        if (series_search_timer !== null) window.clearTimeout(series_search_timer);
        series_search_sequence += 1;
        is_searching_series.value = false;
        return;
    }
    initializeDialog();
});

</script>

<style lang="scss" scoped>

.series-assignment-dialog {
    color: rgb(var(--v-theme-text));
    background: rgb(var(--v-theme-background-lighten-1));

    &__title {
        font-weight: bold;
    }

    &__current,
    &__program {
        display: flex;
        flex-direction: column;
        padding: 11px 13px;
        margin-bottom: 12px;
        border-radius: 7px;
        background: rgb(var(--v-theme-background));

        span {
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 11.5px;
        }

        strong {
            margin-top: 2px;
            font-size: 14px;
            overflow-wrap: anywhere;
        }
    }

    &__rule-note {
        display: flex;
        align-items: flex-start;
        gap: 8px;
        margin-bottom: 14px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 12.5px;
        line-height: 1.5;
    }

    &__hint,
    &__notice {
        margin-top: 10px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 12.5px;
        line-height: 1.55;
    }
}

</style>
