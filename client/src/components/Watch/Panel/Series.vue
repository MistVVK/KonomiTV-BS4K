<template>
    <div class="series-container">
        <div v-if="is_loading" class="series-state">
            <v-progress-circular color="primary" indeterminate size="30" width="3" />
            <span>シリーズ情報を取得しています…</span>
        </div>

        <div v-else-if="series_load_failed" class="series-state">
            <Icon icon="fluent:error-circle-20-regular" width="31px" />
            <span>シリーズ情報を取得できませんでした。</span>
            <v-btn color="primary" size="small" variant="tonal" @click="fetchCurrentSeries()">
                再試行
            </v-btn>
            <v-btn v-if="is_admin" size="small" variant="text" @click="openAssignmentDialog()">
                シリーズを訂正
            </v-btn>
        </div>

        <div v-else-if="is_current_program_loading" class="series-state">
            <v-progress-circular color="primary" indeterminate size="30" width="3" />
            <span>録画情報を取得しています…</span>
        </div>

        <div v-else-if="series_info === null" class="series-state">
            <Icon icon="fluent:video-clip-off-20-regular" width="32px" />
            <span>この録画はシリーズに分類されていません。</span>
            <span class="series-state__description">単発番組として判定された録画もここに含まれます。</span>
            <v-btn v-if="is_admin" color="primary" size="small" variant="tonal" @click="openAssignmentDialog()">
                シリーズを訂正
            </v-btn>
        </div>

        <template v-else>
            <header class="series-header">
                <div class="series-header__main">
                    <h1 class="series-header__title">{{series_info.title}}</h1>
                    <div class="series-header__count">録画 {{recorded_programs.length.toLocaleString()}} 件</div>
                </div>
                <v-btn v-if="is_admin" class="series-header__edit" icon size="small" variant="text"
                    v-ftooltip.bottom="'シリーズを訂正'" @click="openAssignmentDialog()">
                    <Icon icon="fluent:edit-20-filled" width="20px" />
                </v-btn>
            </header>

            <div v-if="recorded_programs.length === 0" class="series-state series-state--compact">
                <span>再生できる録画がありません。</span>
            </div>
            <div v-else class="series-programs" role="list">
                <router-link v-for="program in recorded_programs" :key="program.id" v-ripple
                    class="series-program" :class="{'series-program--current': isCurrentProgram(program)}"
                    :to="`/videos/watch/${program.id}`" :aria-current="isCurrentProgram(program) ? 'page' : undefined"
                    role="listitem">
                    <div class="series-program__episode">
                        <span v-if="program.episode_number !== null">{{program.episode_number}}</span>
                        <span v-else>—</span>
                    </div>
                    <div class="series-program__content">
                        <div class="series-program__heading">
                            <span class="series-program__title">{{getProgramDisplayTitle(program)}}</span>
                            <span v-if="isCurrentProgram(program)" class="series-program__current-label">再生中</span>
                        </div>
                        <div class="series-program__meta">
                            <span>{{formatStartTime(program.start_time)}}</span>
                            <span>{{program.channel?.name ?? 'チャンネル情報なし'}}</span>
                            <span>{{formatDuration(program.duration)}}</span>
                        </div>
                    </div>
                    <Icon class="series-program__arrow" icon="akar-icons:chevron-right" width="17px" />
                </router-link>
            </div>
        </template>

        <v-dialog v-model="show_assignment_dialog" :fullscreen="Utils.isSmartphoneVertical()" max-width="560">
            <v-card class="series-assignment-dialog">
                <v-card-title class="series-assignment-dialog__title">シリーズを訂正</v-card-title>
                <v-card-text>
                    <div class="series-assignment-dialog__current">
                        <span>現在の分類</span>
                        <strong>{{current_assignment_label}}</strong>
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
                    <v-btn variant="text" :disabled="is_assigning" @click="show_assignment_dialog = false">
                        キャンセル
                    </v-btn>
                    <v-btn color="primary" variant="flat" :loading="is_assigning"
                        :disabled="assignment_submit_disabled" @click="submitAssignment()">
                        変更
                    </v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>
    </div>
</template>
<script lang="ts">

import { mapStores } from 'pinia';
import { defineComponent } from 'vue';

import Message from '@/message';
import RecordedSeries, { type IRecordedSeriesAssignment } from '@/services/RecordedSeries';
import Series, { type ISeries } from '@/services/Series';
import Videos, { type IRecordedProgram } from '@/services/Videos';
import usePlayerStore from '@/stores/PlayerStore';
import useUserStore from '@/stores/UserStore';
import Utils, { dayjs } from '@/utils';


type AssignmentMode = 'Existing' | 'New' | 'NotSeries';


export default defineComponent({
    name: 'Panel-SeriesTab',
    data() {
        return {
            // ユーティリティをテンプレートで使えるようにする。
            Utils: Object.freeze(Utils),

            // 表示中の録画が属するシリーズと、その取得状態。
            series_info: null as ISeries | null,
            is_loading: false,
            series_load_failed: false,
            series_fetch_sequence: 0,

            // 管理者向け手動訂正ダイアログの状態。
            show_assignment_dialog: false,
            assignment_mode: 'Existing' as AssignmentMode,
            assignment_series_id: null as number | null,
            assignment_search_query: '',
            assignment_search_results: [] as ISeries[],
            new_series_title: '',
            is_searching_series: false,
            is_assigning: false,
            is_applying_assignment: false,
            series_search_sequence: 0,
            series_search_timer: null as number | null,
        };
    },
    computed: {
        ...mapStores(usePlayerStore, useUserStore),

        /** ルート間でコンポーネントが再利用されても、録画または割り当ての変更を検出できるキー。 */
        recorded_program_identity(): string {
            const program = this.playerStore.recorded_program;
            return `${this.$route.params.video_id ?? 'none'}:${program.id}:${program.series_id ?? 'none'}`;
        },

        /** ルート更新後、PlayerStore が新しい録画へ切り替わるまで旧録画の内容を表示しない。 */
        is_current_program_loading(): boolean {
            const route_program_id = Number(this.$route.params.video_id);
            return (
                Number.isFinite(route_program_id) === false ||
                this.playerStore.recorded_program.id < 0 ||
                this.playerStore.recorded_program.id !== route_program_id
            );
        },

        /** Series API の放送期間を平坦化し、再生可能な録画だけを古い順に並べる。 */
        recorded_programs(): IRecordedProgram[] {
            if (this.series_info === null) return [];

            // reverse relation の prefetch には解析中・失敗した録画も含まれ得るため、Recorded だけを表示する。
            const unique_programs = new Map<number, IRecordedProgram>();
            for (const period of this.series_info.broadcast_periods) {
                for (const program of period.recorded_programs) {
                    if (program.recorded_video.status === 'Recorded') {
                        unique_programs.set(program.id, program);
                    }
                }
            }
            return Array.from(unique_programs.values()).sort((first, second) =>
                dayjs(first.start_time).valueOf() - dayjs(second.start_time).valueOf() || first.id - second.id
            );
        },

        is_admin(): boolean {
            return this.userStore.user?.is_admin === true;
        },

        is_series_tab_active(): boolean {
            return this.playerStore.video_panel_active_tab === 'Series';
        },

        current_assignment_label(): string {
            if (this.series_info !== null) return this.series_info.title;
            if (this.playerStore.recorded_program.series_id !== null) {
                return this.playerStore.recorded_program.series_title ?? 'シリーズ情報を取得できませんでした';
            }
            return 'シリーズなし';
        },

        assignment_submit_disabled(): boolean {
            if (this.is_assigning) return true;
            if (this.assignment_mode === 'Existing') return this.assignment_series_id === null;
            if (this.assignment_mode === 'New') {
                const title = this.new_series_title.trim();
                return title.length === 0 || title.length > 255;
            }
            return false;
        },
    },
    watch: {
        recorded_program_identity: {
            immediate: true,
            handler() {
                // 録画の移動中に、前の録画を対象とした訂正ダイアログを操作させない。
                if (this.show_assignment_dialog && this.is_applying_assignment === false) {
                    this.show_assignment_dialog = false;
                }
                // 手動訂正の成功直後は submitAssignment() 側で再取得するため、同じ API を二重に呼ばない。
                if (this.is_applying_assignment === false) {
                    void this.fetchCurrentSeries();
                }
            },
        },
        is_series_tab_active(is_active: boolean) {
            if (is_active) void this.scrollCurrentProgramIntoView();
        },
    },
    created() {
        // 管理者向けボタンの表示判定に必要。取得済みなら UserStore 内で API 呼び出しは省略される。
        void this.userStore.fetchUser();
    },
    beforeUnmount() {
        // 遅延検索と未完了レスポンスを、このコンポーネントへ反映させない。
        if (this.series_search_timer !== null) window.clearTimeout(this.series_search_timer);
        this.series_fetch_sequence += 1;
        this.series_search_sequence += 1;
    },
    methods: {
        /** 現在の録画に対応する Series API を取得し、古い録画向けレスポンスは破棄する。 */
        async fetchCurrentSeries(): Promise<void> {
            const program_id = this.playerStore.recorded_program.id;
            const series_id = this.playerStore.recorded_program.series_id;
            const route_program_id = Number(this.$route.params.video_id);
            const request_sequence = ++this.series_fetch_sequence;

            // 録画切り替え直後に前のシリーズを見せないよう、リクエスト開始時点で表示を消す。
            this.series_info = null;
            this.series_load_failed = false;
            this.is_loading = program_id >= 0 && program_id === route_program_id && series_id !== null;
            if (program_id < 0 || program_id !== route_program_id || series_id === null) return;

            const fetched_series = await Series.fetchSeries(series_id);
            if (
                request_sequence !== this.series_fetch_sequence ||
                Number(this.$route.params.video_id) !== program_id ||
                this.playerStore.recorded_program.id !== program_id ||
                this.playerStore.recorded_program.series_id !== series_id
            ) {
                return;
            }

            this.is_loading = false;
            if (fetched_series === null) {
                this.series_load_failed = true;
                return;
            }
            this.series_info = fetched_series;
            if (this.is_series_tab_active) await this.scrollCurrentProgramIntoView();
        },

        /** 長いシリーズでは、タブを開いたときに現在再生中の話が見える位置まで移動する。 */
        async scrollCurrentProgramIntoView(): Promise<void> {
            await this.$nextTick();
            const current_program = (this.$el as HTMLElement).querySelector<HTMLElement>('.series-program--current');
            current_program?.scrollIntoView({block: 'center'});
        },

        isCurrentProgram(program: IRecordedProgram): boolean {
            return program.id === this.playerStore.recorded_program.id;
        },

        getProgramDisplayTitle(program: IRecordedProgram): string {
            const subtitle = program.subtitle?.trim();
            if (subtitle) return subtitle;
            return program.title;
        },

        formatStartTime(start_time: string): string {
            return dayjs(start_time).format('YYYY/M/D (ddd) HH:mm');
        },

        formatDuration(duration_seconds: number): string {
            return `${Math.max(1, Math.round(duration_seconds / 60)).toLocaleString()}分`;
        },

        /** 訂正ダイアログを、現在の割り当てを初期選択した状態で開く。 */
        openAssignmentDialog(): void {
            if (this.is_admin === false) return;

            this.assignment_mode = 'Existing';
            this.assignment_series_id = this.playerStore.recorded_program.series_id;
            this.assignment_search_query = '';
            this.assignment_search_results = this.series_info === null ? [] : [this.series_info];
            this.new_series_title = this.playerStore.recorded_program.series_title ?? '';
            this.show_assignment_dialog = true;
            void this.searchSeries('');
        },

        /** 入力中の検索を短時間まとめ、古い検索結果が新しい検索語を上書きしないようにする。 */
        queueSeriesSearch(query: string | null): void {
            if (this.series_search_timer !== null) window.clearTimeout(this.series_search_timer);
            this.series_search_timer = window.setTimeout(() => {
                this.series_search_timer = null;
                void this.searchSeries(query?.trim() ?? '');
            }, 300);
        },

        /** 空文字では最近更新されたシリーズ、入力時は一致する既存シリーズを取得する。 */
        async searchSeries(query: string): Promise<void> {
            const request_sequence = ++this.series_search_sequence;
            this.is_searching_series = true;
            const result = query === '' ?
                await Series.fetchSeriesList('desc', 1) :
                await Series.searchSeries(query, 'desc', 1);
            if (request_sequence !== this.series_search_sequence) return;

            this.is_searching_series = false;
            if (result === null) return;

            // 現在のシリーズが検索上位30件から外れていても、初期選択だけは失わないようにする。
            const series_list = [...result.series_list];
            if (this.series_info !== null && series_list.some(series => series.id === this.series_info?.id) === false) {
                series_list.unshift(this.series_info);
            }
            this.assignment_search_results = series_list;
        },

        /** 選択した訂正を保存し、現在の録画とシリーズ一覧をサーバーから取り直す。 */
        async submitAssignment(): Promise<void> {
            if (this.assignment_submit_disabled || this.is_admin === false) return;

            let assignment: IRecordedSeriesAssignment;
            if (this.assignment_mode === 'Existing') {
                if (this.assignment_series_id === null) return;
                assignment = {decision: 'Series', series_id: this.assignment_series_id};
            } else if (this.assignment_mode === 'New') {
                assignment = {decision: 'Series', series_title: this.new_series_title.trim()};
            } else {
                assignment = {decision: 'NotSeries'};
            }

            const program_id = this.playerStore.recorded_program.id;
            this.is_assigning = true;
            const succeeded = await RecordedSeries.updateProgramAssignment(program_id, assignment);
            if (succeeded === false) {
                this.is_assigning = false;
                return;
            }

            // API は 204 を返すため、更新後の ID・表示名・話数情報は録画 API を正として取り直す。
            const refreshed_program = await Videos.fetchVideo(program_id);
            if (refreshed_program === null) {
                this.is_assigning = false;
                this.show_assignment_dialog = false;
                Message.warning('変更は保存されましたが、表示を更新できませんでした。ページを再読み込みしてください。');
                return;
            }
            if (this.playerStore.recorded_program.id === program_id) {
                this.is_applying_assignment = true;
                try {
                    this.playerStore.recorded_program = refreshed_program;
                    await this.fetchCurrentSeries();
                } finally {
                    this.is_applying_assignment = false;
                }
            }

            this.is_assigning = false;
            this.show_assignment_dialog = false;
            Message.success(assignment.decision === 'NotSeries' ?
                '単発番組へ変更しました。' :
                '録画番組のシリーズを変更しました。');
        },
    },
});

</script>
<style lang="scss" scoped>

.series-container {
    padding: 0 16px 18px;
    overflow-y: auto;
    @include tablet-vertical {
        padding: 18px 24px 24px;
    }
    @include smartphone-horizontal {
        padding: 10px 12px 12px;
    }
    @include smartphone-vertical {
        padding: 14px 12px 20px;
    }
}

.series-state {
    display: flex;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    min-height: 190px;
    gap: 13px;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 13px;
    text-align: center;

    &--compact {
        min-height: 120px;
    }

    &__description {
        max-width: 280px;
        margin-top: -6px;
        font-size: 11.5px;
        line-height: 1.6;
    }
}

.series-header {
    display: flex;
    align-items: flex-start;
    position: sticky;
    top: 0;
    padding: 15px 0 12px;
    background: rgb(var(--v-theme-background));
    z-index: 2;
    @include tablet-vertical {
        padding-top: 0;
    }
    @include smartphone-horizontal {
        padding: 0 0 8px;
    }

    &__main {
        min-width: 0;
        flex-grow: 1;
    }

    &__title {
        margin: 0;
        font-size: 20px;
        font-weight: bold;
        line-height: 1.4;
        overflow-wrap: anywhere;
        @include smartphone-horizontal {
            font-size: 16px;
        }
        @include smartphone-vertical {
            font-size: 18px;
        }
    }

    &__count {
        margin-top: 3px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 11.5px;
    }

    &__edit {
        flex-shrink: 0;
        margin: -3px -7px 0 5px;
    }
}

.series-programs {
    border-radius: 6px;
    overflow: hidden;
}

.series-program {
    display: flex;
    align-items: center;
    min-height: 68px;
    padding: 9px 8px;
    border-bottom: 1px solid rgba(var(--v-theme-text), 0.1);
    color: rgb(var(--v-theme-text));
    background: rgb(var(--v-theme-background-lighten-1));
    text-decoration: none;
    transition: background-color 0.15s;
    user-select: none;
    cursor: pointer;
    @include smartphone-horizontal {
        min-height: 58px;
        padding: 6px;
    }

    &:last-child {
        border-bottom: none;
    }

    &:hover {
        background: rgb(var(--v-theme-background-lighten-2));
    }

    &--current {
        box-shadow: inset 4px 0 rgb(var(--v-theme-primary));
        background: rgba(var(--v-theme-primary), 0.13);
    }

    &__episode {
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
        width: 62px;
        padding: 0 6px;
        color: rgb(var(--v-theme-primary-readable));
        font-size: 12px;
        font-weight: bold;
        line-height: 1.35;
        text-align: center;
        overflow-wrap: anywhere;
        @include smartphone-horizontal {
            width: 52px;
            font-size: 10.5px;
        }
    }

    &__content {
        min-width: 0;
        flex-grow: 1;
        padding-left: 5px;
    }

    &__heading {
        display: flex;
        align-items: center;
        min-width: 0;
        gap: 7px;
    }

    &__title {
        min-width: 0;
        font-size: 13px;
        font-weight: 600;
        line-height: 1.45;
        overflow: hidden;
        white-space: nowrap;
        text-overflow: ellipsis;
        @include smartphone-horizontal {
            font-size: 11.5px;
        }
    }

    &__current-label {
        flex-shrink: 0;
        padding: 2px 5px;
        border-radius: 3px;
        color: rgb(var(--v-theme-on-primary));
        background: rgb(var(--v-theme-primary));
        font-size: 9.5px;
        font-weight: bold;
        line-height: 1.25;
    }

    &__meta {
        display: flex;
        align-items: center;
        min-width: 0;
        margin-top: 4px;
        gap: 0;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 10.5px;
        line-height: 1.4;
        white-space: nowrap;
        overflow: hidden;

        span {
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        span + span::before {
            margin: 0 5px;
            content: '・';
        }

        span:nth-child(2) {
            flex-shrink: 1;
        }

        span:first-child,
        span:last-child {
            flex-shrink: 0;
        }

        @include smartphone-horizontal {
            font-size: 9.5px;
        }
    }

    &__arrow {
        flex-shrink: 0;
        margin-left: 4px;
        color: rgb(var(--v-theme-text-darken-1));
    }
}

.series-assignment-dialog {
    color: rgb(var(--v-theme-text));
    background: rgb(var(--v-theme-background-lighten-1));

    &__title {
        font-weight: bold;
    }

    &__current {
        display: flex;
        flex-direction: column;
        margin-bottom: 12px;
        padding: 11px 13px;
        border-radius: 6px;
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

    &__hint,
    &__notice {
        margin-top: 8px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 11.5px;
        line-height: 1.6;
    }

    &__notice {
        padding: 11px 13px;
        border-radius: 6px;
        background: rgb(var(--v-theme-background));
    }

    &__rule-note {
        display: flex;
        align-items: flex-start;
        gap: 7px;
        margin-bottom: 8px;
        padding: 9px 11px;
        border-radius: 6px;
        color: rgb(var(--v-theme-text-darken-1));
        background: rgba(var(--v-theme-primary), 0.1);
        font-size: 11.5px;
        line-height: 1.55;

        svg {
            flex-shrink: 0;
            margin-top: 1px;
            color: rgb(var(--v-theme-primary-readable));
        }
    }
}

</style>
