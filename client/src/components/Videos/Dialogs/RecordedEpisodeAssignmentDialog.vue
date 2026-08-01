<template>
    <v-dialog :model-value="modelValue" :fullscreen="Utils.isSmartphoneVertical()"
        :persistent="is_dialog_busy"
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
                        <strong>{{episodeResolutionStatusLabel(target_program.resolution.status)}}</strong>
                    </div>
                    <div>
                        <span>Web 検索結果</span>
                        <strong>{{episodeLookupOutcomeDisplayLabel(target_program.resolution)}}</strong>
                    </div>
                    <div v-if="target_program.resolution.source !== null">
                        <span>判定元</span>
                        <strong>{{episodeResolutionSourceLabel(target_program.resolution.source)}}</strong>
                    </div>
                    <div v-if="target_program.resolution.confidence !== null">
                        <span>信頼度</span>
                        <strong>{{Math.round(target_program.resolution.confidence * 100)}}%</strong>
                    </div>
                    <div>
                        <span>AI レーン（提案話数）</span>
                        <strong>{{proposed_episode_label}}</strong>
                        <v-btn v-if="resolution_proposal !== null" class="mt-2" color="primary"
                            variant="tonal" size="x-small" :disabled="is_dialog_busy"
                            @click="applyResolutionProposal()">
                            提案を入力欄へ反映
                        </v-btn>
                    </div>
                    <div v-if="manual_lane_label !== null">
                        <span>手動レーン</span>
                        <strong>{{manual_lane_label}}</strong>
                    </div>
                    <div>
                        <span>Web 検索の実行</span>
                        <strong>{{target_program.resolution.web_search_performed ? '実行済み' : '未実行'}}</strong>
                    </div>
                    <div v-if="target_program.resolution.citations.length > 0">
                        <span>Web 出典</span>
                        <strong>{{target_program.resolution.citations.length.toLocaleString()}} 件</strong>
                    </div>
                </div>
                <div v-if="resolution_rationale !== null"
                    class="recorded-episode-dialog__reason mt-2">
                    <span>判定根拠</span>
                    <p>{{resolution_rationale}}</p>
                </div>
                <div v-if="resolution_error_message !== null"
                    class="recorded-episode-dialog__reason mt-2">
                    <span>エラー内容</span>
                    <p>{{resolution_error_message}}</p>
                    <small v-if="target_program.resolution !== null &&
                        target_program.resolution.error_code !== null">
                        エラーコード: <code>{{target_program.resolution.error_code}}</code>
                    </small>
                </div>
                <div v-if="target_program.resolution !== null &&
                    resolution_rationale === null &&
                    resolution_error_message === null"
                    class="recorded-episode-dialog__reason mt-2">
                    <span>判定理由</span>
                    <p>{{recordedEpisodeResolutionReason(target_program.resolution)}}</p>
                </div>
                <div v-if="target_program.resolution?.citations.length" class="recorded-episode-dialog__citations mt-2">
                    <span>判定に使った Web 出典</span>
                    <a v-for="citation in target_program.resolution.citations" :key="citation.url"
                        :href="citation.url" target="_blank" rel="noopener noreferrer">
                        <Icon icon="fluent:open-20-regular" width="15px" />
                        {{citation.title || citation.url}}
                    </a>
                </div>

                <div class="recorded-episode-dialog__relookup mt-3">
                    <div>
                        <strong>AI で再検索</strong>
                        <span>
                            この録画1件の Web 検索です。受理結果は AI レーンへ保存され、正本としても自動採用されます。
                            手動レーンの値は残ります。追加の AI API 利用が発生します。
                        </span>
                    </div>
                    <v-btn color="background-lighten-2" variant="flat" size="small"
                        :loading="is_starting_relookup"
                        :disabled="is_saving || is_starting_relookup || is_monitoring_relookup"
                        @click="relookup_confirmation_dialog = true">
                        <Icon icon="fluent:globe-search-20-filled" class="mr-2" width="18px" />
                        {{target_program.resolution?.lookup_outcome === 'Pending' ?
                            '再検索の進捗を引き継ぐ' : 'AI で再検索'}}
                    </v-btn>
                    <v-progress-linear v-if="is_episode_lookup_pending" color="primary" height="6" rounded
                        :indeterminate="relookup_progress === null"
                        :model-value="relookup_progress ?? undefined" />
                    <small v-if="is_episode_lookup_pending">
                        {{relookup_execution === null ? '話数 Web 検索を実行しています…' :
                            stageLabel(relookup_execution.stage)}}
                        <template v-if="relookup_progress !== null">・{{relookup_progress.toFixed(0)}}%</template>
                    </small>
                </div>

                <v-radio-group class="mt-3" v-model="assignment_mode" color="primary" hide-details>
                    <v-radio label="登録済みの話数を選ぶ" value="Existing" />
                    <v-radio label="シーズン・話数を入力する" value="Structured" />
                    <v-radio label="話数不明として確定する" value="Unknown" />
                    <v-radio label="AI 検索結果を採用する" value="AdoptAI"
                        :disabled="can_adopt_ai === false" />
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

                <div v-else-if="assignment_mode === 'AdoptAI'" class="recorded-episode-dialog__notice mt-3">
                    <template v-if="target_program?.resolution?.lookup_outcome === 'NotNumbered'">
                        保存済みの AI 判定「公式話数なし」を正本として採用します。手動レーンの値は残ります。
                    </template>
                    <template v-else>
                        保存済みの AI 提案（{{proposed_episode_label}}）を正本として採用します。手動レーンの値は残ります。
                    </template>
                </div>

                <div v-else class="recorded-episode-dialog__notice mt-3">
                    自動判定で再び上書きされないよう、この録画は管理者が「話数不明」と判断した結果として保存します。
                    AI レーンの検索結果は残ります。
                </div>

                <div class="recorded-episode-dialog__note mt-3">
                    <Icon icon="fluent:info-20-regular" width="18px" />
                    <span>同じシーズン・話数を指定した複数局の録画は、同一話としてまとめたまま個別の録画として残ります。</span>
                </div>
            </v-card-text>

            <v-card-actions>
                <v-spacer />
                <v-btn variant="text" :disabled="is_dialog_busy"
                    @click="updateDialog(false)">キャンセル</v-btn>
                <v-btn v-if="target_program !== null" color="primary" variant="flat" :loading="is_saving"
                    :disabled="submit_disabled" @click="saveAssignment()">
                    保存
                </v-btn>
            </v-card-actions>
        </v-card>
    </v-dialog>

    <v-dialog v-model="relookup_confirmation_dialog" :persistent="is_starting_relookup" max-width="540">
        <v-card class="recorded-episode-dialog">
            <v-card-title>AI で話数を再検索</v-card-title>
            <v-card-text>
                この録画1件を対象に、現在の AI バックエンドで Web 検索を実行します。<br>
                追加の AI API 利用が発生します。検索失敗時は現在の正本話数を維持します。<br>
                <strong class="d-block mt-3">
                    受理された判定は AI レーンへ保存し、正本としても自動採用します。手動レーンは残ります。
                </strong>
                <strong class="d-block mt-3">現在の正本: {{current_episode_label}}</strong>
            </v-card-text>
            <v-card-actions>
                <v-spacer />
                <v-btn variant="text" :disabled="is_starting_relookup"
                    @click="relookup_confirmation_dialog = false">キャンセル</v-btn>
                <v-btn color="primary" variant="flat" :loading="is_starting_relookup"
                    @click="startEpisodeRelookup()">
                    再検索する
                </v-btn>
            </v-card-actions>
        </v-card>
    </v-dialog>
</template>

<script setup lang="ts">

import { computed, onUnmounted, ref, watch } from 'vue';

import Message from '@/message';
import AnalysisTasks, { type IAnalysisTaskExecution } from '@/services/AnalysisTasks';
import RecordedSeries, {
    type IRecordedEpisodeAssignmentList,
    type IRecordedEpisodeAssignmentProgram,
    type IRecordedEpisodeAssignmentUpdate,
} from '@/services/RecordedSeries';
import { stageLabel } from '@/stores/AnalysisTasksStore';
import Utils, { dayjs } from '@/utils';
import { formatRecordedEpisodeNumber } from '@/utils/RecordedEpisode';
import {
    canAdoptAIEpisodeResolution,
    canCloseEpisodeAssignmentDialog,
    episodeLookupOutcomeDisplayLabel,
    episodeResolutionSourceLabel,
    episodeResolutionStatusLabel,
    markResolutionLookupPending,
    recordedEpisodeResolutionErrorMessage,
    recordedEpisodeResolutionProposal,
    recordedEpisodeResolutionRationale,
    recordedEpisodeResolutionReason,
} from '@/utils/RecordedEpisodeResolution';


type AssignmentMode = 'Existing' | 'Structured' | 'Unknown' | 'AdoptAI';

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
const is_starting_relookup = ref(false);
const is_monitoring_relookup = ref(false);
const relookup_confirmation_dialog = ref(false);
const relookup_execution = ref<IAnalysisTaskExecution | null>(null);
const assignment_mode = ref<AssignmentMode>('Structured');
const selected_episode_id = ref<number | null>(null);
const season_number = ref<number | string>(1);
const episode_number = ref('');
let request_sequence = 0;
let relookup_abort_controller: AbortController | null = null;
let relookup_initial_assignment_draft: string | null = null;

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
const proposed_episode_label = computed(() => {
    const resolution = target_program.value?.resolution;
    if (resolution === null || resolution === undefined) return '提案なし';
    if (resolution.proposed_season_number === null && resolution.proposed_episode_number === null) return '提案なし';
    if (resolution.proposed_episode_number === null) {
        return `シーズン ${resolution.proposed_season_number ?? '未提示'}・話数未提示`;
    }
    if (resolution.proposed_season_number === null) {
        return `シーズン未提示・第 ${formatRecordedEpisodeNumber(resolution.proposed_episode_number)} 話`;
    }
    return formatEpisodeLabel(resolution.proposed_season_number, resolution.proposed_episode_number);
});
/** 手動レーンが保存されているときだけ表示するラベル。正本 source とは独立。 */
const manual_lane_label = computed(() => {
    const resolution = target_program.value?.resolution;
    if (resolution === null || resolution === undefined || resolution.manual_status === null) {
        return null;
    }
    if (resolution.manual_status === 'Unknown') return '話数不明として手動確定';
    if (resolution.manual_status === 'NotNumbered') return '公式話数なしとして手動確定';
    if (
        resolution.manual_season_number !== null &&
        resolution.manual_episode_number !== null
    ) {
        return formatEpisodeLabel(resolution.manual_season_number, resolution.manual_episode_number);
    }
    return '手動確定済み（話数詳細なし）';
});
const resolution_proposal = computed(() => recordedEpisodeResolutionProposal(target_program.value?.resolution));
const can_adopt_ai = computed(() => canAdoptAIEpisodeResolution(target_program.value?.resolution));
const is_manual_resolution = computed(() => target_program.value?.resolution?.source === 'Manual');
const is_dialog_busy = computed(() => canCloseEpisodeAssignmentDialog(
    is_saving.value,
    is_starting_relookup.value,
    is_monitoring_relookup.value,
) === false);
const resolution_rationale = computed(() => {
    const resolution = target_program.value?.resolution;
    return resolution === null || resolution === undefined ? null : recordedEpisodeResolutionRationale(resolution);
});
const resolution_error_message = computed(() => {
    const resolution = target_program.value?.resolution;
    return resolution === null || resolution === undefined ? null : recordedEpisodeResolutionErrorMessage(resolution);
});
const relookup_progress = computed(() => {
    if (relookup_execution.value?.progress === null || relookup_execution.value?.progress === undefined) return null;
    return Math.max(0, Math.min(100, relookup_execution.value.progress * 100));
});
const is_episode_lookup_pending = computed(() =>
    is_starting_relookup.value ||
    is_monitoring_relookup.value ||
    target_program.value?.resolution?.lookup_outcome === 'Pending',
);
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
    if (is_dialog_busy.value || target_program.value === null) return true;
    if (assignment_mode.value === 'Existing') return selected_episode_id.value === null;
    if (assignment_mode.value === 'Structured') {
        return season_number_error.value !== '' || episode_number_error.value !== '';
    }
    if (assignment_mode.value === 'AdoptAI') return can_adopt_ai.value === false;
    return false;
});


function formatEpisodeLabel(season: number, episode: string): string {
    return `シーズン ${season}・第 ${formatRecordedEpisodeNumber(episode)} 話`;
}

function assignmentDraftFingerprint(): string {
    return JSON.stringify([
        assignment_mode.value,
        selected_episode_id.value,
        String(season_number.value),
        episode_number.value,
    ]);
}

/** AI の完全な提案を編集欄へ移し、管理者が確認してから既存の保存 API で確定できるようにする。 */
function applyResolutionProposal(): void {
    const proposal = resolution_proposal.value;
    if (proposal === null || is_dialog_busy.value) return;

    assignment_mode.value = 'Structured';
    selected_episode_id.value = null;
    season_number.value = proposal.seasonNumber;
    episode_number.value = proposal.episodeNumber;
    Message.info('AI の提案を入力欄へ反映しました。内容を確認して保存してください。');
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

/** 202 応答直後から現在値を維持したまま lookup 監査だけを Pending として表示する。 */
function markEpisodeRelookupPending(recordedProgramId: number): void {
    const currentAssignments = assignments.value;
    if (currentAssignments === null) return;

    let updated = false;
    const programs = currentAssignments.programs.map((program) => {
        if (program.recorded_program_id !== recordedProgramId || program.resolution === null) return program;
        updated = true;
        return {
            ...program,
            // サーバー開始時と同じく error / 根拠を消し、evidence は前回成功分を残す。
            resolution: markResolutionLookupPending(program.resolution),
        };
    });
    if (updated === false) return;

    assignments.value = {...currentAssignments, programs};
    emit('saved', assignments.value);
}

/** 再検索完了後の確定値と安全な理由を assignment API から再取得する。 */
async function refreshAssignmentsAfterRelookup(recordedProgramId: number): Promise<boolean> {
    const seriesId = props.seriesId;
    const sequence = ++request_sequence;
    const refreshed = await RecordedSeries.fetchEpisodeAssignments(seriesId, false);
    if (
        sequence !== request_sequence ||
        props.seriesId !== seriesId ||
        props.recordedProgramId !== recordedProgramId ||
        refreshed === null
    ) {
        return false;
    }

    const draftWasEdited = (
        relookup_initial_assignment_draft !== null &&
        assignmentDraftFingerprint() !== relookup_initial_assignment_draft
    );
    assignments.value = refreshed;
    if (draftWasEdited === false) initializeDraft();
    relookup_initial_assignment_draft = null;
    emit('saved', refreshed);
    return true;
}

/** 明示確認後に1件再検索を開始し、AnalysisTask の終端までキャンセル可能なポーリングを行う。 */
async function startEpisodeRelookup(): Promise<void> {
    const program = target_program.value;
    if (
        program === null ||
        is_starting_relookup.value ||
        is_monitoring_relookup.value
    ) {
        return;
    }

    const recordedProgramId = program.recorded_program_id;
    is_starting_relookup.value = true;
    const result = await RecordedSeries.startEpisodeRelookup(recordedProgramId, {
        expected_series_id: props.seriesId,
        expected_series_episode_id: program.series_episode_id,
        override_manual: is_manual_resolution.value,
    });
    is_starting_relookup.value = false;
    relookup_confirmation_dialog.value = false;

    if (result.type === 'NotFound') {
        Message.error('録画が見つからないか、再生可能な状態ではありません。最新情報を読み込み直してください。');
        await loadAssignments();
        return;
    }
    if (result.type === 'Conflict') {
        Message.error('同じ録画の再検索が実行中か、シリーズまたは話数が変更されています。最新情報を読み込み直しました。');
        await loadAssignments();
        return;
    }
    if (result.type === 'RateLimited') {
        Message.warning('本日の AI API 利用上限、またはプロバイダーの利用上限に達しています。');
        await loadAssignments();
        return;
    }
    if (result.type === 'Unavailable') {
        Message.error('現在の設定または AI バックエンドでは、話数 Web 検索を実行できません。');
        await loadAssignments();
        return;
    }
    if (result.type === 'Error') return;

    relookup_abort_controller?.abort();
    const abortController = new AbortController();
    relookup_abort_controller = abortController;
    relookup_execution.value = null;
    is_monitoring_relookup.value = true;
    relookup_initial_assignment_draft = assignmentDraftFingerprint();
    markEpisodeRelookupPending(recordedProgramId);
    Message.info(result.task.reused ? '実行中の話数再検索を引き続き監視します。' : '話数の AI 再検索を開始しました。');

    const execution = await AnalysisTasks.waitForCompletion(
        result.task.execution_id,
        abortController.signal,
        task => relookup_execution.value = task,
    );
    if (abortController.signal.aborted) return;

    is_monitoring_relookup.value = false;
    relookup_execution.value = execution;
    const refreshed = await refreshAssignmentsAfterRelookup(recordedProgramId);
    relookup_initial_assignment_draft = null;
    if (refreshed === false) {
        Message.warning('話数再検索は終了しましたが、最新の判定結果を取得できませんでした。');
    }
    if (execution?.status === 'Succeeded') {
        Message.success('話数の AI 再検索が完了しました。');
    } else if (execution?.status === 'Interrupted') {
        Message.warning('話数の AI 再検索が中断されました。現在の話数は維持されています。');
    } else if (execution !== null) {
        Message.error('話数の AI 再検索に失敗しました。現在の話数は維持されています。');
    }
}

function updateDialog(value: boolean): void {
    if (value === false && is_dialog_busy.value) return;
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
    } else if (assignment_mode.value === 'AdoptAI') {
        update = {...common, decision: 'AdoptAI'};
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
    if (assignment_mode.value === 'Unknown') {
        Message.success('話数不明として保存しました。');
    } else if (assignment_mode.value === 'AdoptAI') {
        Message.success('AI 検索結果を正本として採用しました。');
    } else {
        Message.success('録画の話数を更新しました。');
    }
}

watch(() => props.modelValue, (is_open) => {
    if (is_open) {
        relookup_execution.value = null;
        void loadAssignments();
    } else {
        // 親ダイアログ側から閉じる更新が来ても、監視中は再表示して polling を継続する。
        if (is_dialog_busy.value) {
            emit('update:modelValue', true);
            return;
        }
        relookup_confirmation_dialog.value = false;
        relookup_abort_controller?.abort();
        relookup_abort_controller = null;
        is_monitoring_relookup.value = false;
        relookup_initial_assignment_draft = null;
        request_sequence += 1;
        is_loading.value = false;
        load_failed.value = false;
    }
});

onUnmounted(() => {
    relookup_abort_controller?.abort();
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

    &__reason {
        padding: 9px 11px;
        border-radius: 6px;
        background: rgb(var(--v-theme-background));

        > span,
        > small {
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 10.5px;
        }

        > p {
            margin: 4px 0 0;
            font-size: 12px;
            line-height: 1.6;
            overflow-wrap: anywhere;
        }

        > small {
            display: block;
            margin-top: 5px;

            code {
                overflow-wrap: anywhere;
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

    &__relookup {
        display: grid;
        align-items: center;
        grid-template-columns: minmax(0, 1fr) auto;
        padding: 10px 12px;
        gap: 9px 12px;
        border-radius: 6px;
        background: rgba(var(--v-theme-primary), 0.09);

        > div {
            display: flex;
            flex-direction: column;
            min-width: 0;

            strong {
                font-size: 12.5px;
            }

            span {
                margin-top: 3px;
                color: rgb(var(--v-theme-text-darken-1));
                font-size: 10.5px;
                line-height: 1.5;
            }
        }

        > .v-progress-linear,
        > small {
            grid-column: 1 / -1;
        }

        > small {
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 10.5px;
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

        &__relookup {
            grid-template-columns: minmax(0, 1fr);

            > .v-btn {
                justify-self: start;
            }
        }
    }
}

</style>
