<template>
    <div class="series-container">
        <div class="series-controls">
            <v-btn-toggle v-model="filter_mode" class="series-controls__toggle" color="primary"
                aria-label="検索範囲" density="compact" mandatory variant="outlined">
                <v-btn size="small" value="strict">
                    <Icon class="mr-1" icon="fluent:collections-20-regular" width="16px" />
                    同シリーズ
                </v-btn>
                <v-btn size="small" value="relaxed">
                    <Icon class="mr-1" icon="fluent:apps-20-regular" width="16px" />
                    関連番組
                </v-btn>
            </v-btn-toggle>
            <v-checkbox v-model="show_other_channels" class="series-controls__checkbox" color="primary"
                density="compact" hide-details label="他CH含む" />
            <v-btn v-if="current_series_path !== null" class="series-controls__series-link"
                :to="current_series_path" color="primary" size="small" variant="tonal">
                <Icon class="mr-1" icon="fluent:open-20-regular" width="16px" />
                カタログ
            </v-btn>
        </div>

        <div v-if="is_current_program_loading" class="series-state">
            <v-progress-circular color="primary" indeterminate size="30" width="3" />
            <span>録画情報を取得しています…</span>
        </div>
        <div v-else-if="is_loading" class="series-state">
            <v-progress-circular color="primary" indeterminate size="30" width="3" />
            <span>シリーズ番組を検索しています…</span>
        </div>
        <div v-else-if="load_failed" class="series-state">
            <Icon icon="fluent:error-circle-20-regular" width="31px" />
            <span>関連番組を取得できませんでした。</span>
            <v-btn color="primary" size="small" variant="tonal" @click="searchRelatedPrograms()">
                再試行
            </v-btn>
        </div>
        <template v-else>
            <div v-if="related_programs.length > 0" class="series-info">
                {{total.toLocaleString()}} 件のシリーズ番組
            </div>
            <div v-if="related_programs.length > 0" class="series-list" role="list">
                <router-link v-for="program in related_programs" :key="program.id" v-ripple
                    class="series-item" :class="{'series-item--current': isCurrentProgram(program)}"
                    :to="`/videos/watch/${program.id}`" :aria-current="isCurrentProgram(program) ? 'page' : undefined"
                    role="listitem">
                    <div v-if="episodeNumberLabel(program) !== ''" class="series-item__episode">
                        {{episodeNumberLabel(program)}}
                    </div>
                    <div class="series-item__heading">
                        <div class="series-item__title">{{episodeTitle(program)}}</div>
                        <span v-if="isCurrentProgram(program)" class="series-item__current-label">再生中</span>
                    </div>
                    <div class="series-item__thumbnail">
                        <img class="series-item__thumbnail-image" decoding="async" loading="lazy"
                            :src="`${Utils.api_base_url}/videos/${program.id}/thumbnail`" alt="">
                        <div class="series-item__thumbnail-duration">{{ProgramUtils.getProgramDuration(program)}}</div>
                        <div v-if="program.recorded_video.status === 'Recording'"
                            class="series-item__thumbnail-status">
                            <span class="series-item__thumbnail-status-dot"></span>
                            録画中
                        </div>
                    </div>
                    <div class="series-item__meta">
                        <div v-if="program.channel !== null" class="series-item__channel">
                            <div class="series-item__channel-logo">
                                <img loading="lazy" :src="`${Utils.api_base_url}/channels/${program.channel.id}/logo`" alt="">
                            </div>
                            <span>{{program.channel.name}}</span>
                        </div>
                        <span class="series-item__date">{{formatDate(program.start_time)}}</span>
                    </div>
                </router-link>
            </div>
            <div v-else class="series-state">
                <Icon icon="fluent:tv-20-regular" width="42px" />
                <span>シリーズ番組が見つかりません。</span>
                <span v-if="filter_mode === 'strict'" class="series-state__description">
                    「関連番組」へ切り替えて再度お試しください。
                </span>
            </div>
            <div v-if="has_more" class="series-load-more">
                <v-btn :loading="is_loading_more" color="primary" variant="text" @click="loadMore()">
                    <Icon v-if="is_loading_more === false" class="mr-1"
                        icon="fluent:arrow-download-20-regular" width="18px" />
                    さらに読み込む
                </v-btn>
            </div>
        </template>
    </div>
</template>
<script lang="ts">

import { mapStores } from 'pinia';
import { defineComponent } from 'vue';

import Videos, { type IRecordedProgram } from '@/services/Videos';
import usePlayerStore from '@/stores/PlayerStore';
import Utils, { dayjs } from '@/utils';
import { ProgramUtils } from '@/utils/ProgramUtils';
import { extractValidEpisodeSubtitle, formatRecordedEpisodeLabel, formatRecordedEpisodeNumber } from '@/utils/RecordedEpisode';


export default defineComponent({
    name: 'Panel-SeriesTab',
    data() {
        return {
            // テンプレートから参照する表示ユーティリティ。
            Utils: Object.freeze(Utils),
            ProgramUtils: Object.freeze(ProgramUtils),

            // HonomiTV と同じ検索条件。変更時はサーバー側で候補を取り直す。
            filter_mode: 'strict' as 'strict' | 'relaxed',
            show_other_channels: false,

            // 現在の条件で取得したリストとページング状態。
            related_programs: [] as IRecordedProgram[],
            total: 0,
            fetched_pages: 0,

            // 検索・追加取得の表示状態と、古い録画向け応答を破棄する世代番号。
            is_loading: false,
            is_loading_more: false,
            load_failed: false,
            request_sequence: 0,
            active_request_key: null as string | null,
        };
    },
    computed: {
        ...mapStores(usePlayerStore),

        current_series_path(): string | null {
            const series_id = this.playerStore.recorded_program.series_id;
            return series_id === null ? null : `/series/${series_id}`;
        },

        /** ルート間でコンポーネントが再利用されても、録画の切り替わりを検出できるキー。 */
        recorded_program_identity(): string {
            const program = this.playerStore.recorded_program;
            return `${this.$route.params.video_id ?? 'none'}:${program.id}:${program.series_id ?? 'none'}`;
        },

        /** ルート更新後、PlayerStore が新しい録画へ切り替わるまで旧録画のリストを表示しない。 */
        is_current_program_loading(): boolean {
            const route_program_id = Number(this.$route.params.video_id);
            return (
                Number.isInteger(route_program_id) === false ||
                this.playerStore.recorded_program.id < 0 ||
                this.playerStore.recorded_program.id !== route_program_id
            );
        },

        is_series_tab_active(): boolean {
            return this.playerStore.video_panel_active_tab === 'Series';
        },

        has_more(): boolean {
            return this.related_programs.length < this.total;
        },
    },
    watch: {
        recorded_program_identity: {
            immediate: true,
            handler() {
                // 旧録画向けの取得を無効化し、Series タブを表示中なら新しい録画を検索する。
                this.request_sequence += 1;
                this.active_request_key = null;
                this.related_programs = [];
                this.total = 0;
                this.fetched_pages = 0;
                if (this.is_series_tab_active) void this.searchRelatedPrograms();
            },
        },
        is_series_tab_active(is_active: boolean) {
            if (is_active) void this.searchRelatedPrograms();
        },
        filter_mode() {
            if (this.is_series_tab_active) void this.searchRelatedPrograms();
        },
        show_other_channels() {
            if (this.is_series_tab_active) void this.searchRelatedPrograms();
        },
    },
    beforeUnmount() {
        this.request_sequence += 1;
    },
    methods: {
        /** 現在の録画と検索条件に対応する関連番組の先頭ページを取得する。 */
        async searchRelatedPrograms(): Promise<void> {
            if (this.is_series_tab_active === false || this.is_current_program_loading) return;

            const program_id = this.playerStore.recorded_program.id;
            const request_key = `${program_id}:${this.filter_mode}:${this.show_other_channels}`;
            if (this.is_loading && this.active_request_key === request_key) return;

            const request_sequence = ++this.request_sequence;
            this.active_request_key = request_key;
            this.is_loading = true;
            this.is_loading_more = false;
            this.load_failed = false;
            this.related_programs = [];
            this.total = 0;
            this.fetched_pages = 0;

            const result = await Videos.fetchRelatedVideos(
                program_id,
                this.filter_mode,
                this.show_other_channels,
                'desc',
                1,
            );
            if (
                request_sequence !== this.request_sequence ||
                this.playerStore.recorded_program.id !== program_id ||
                this.active_request_key !== request_key
            ) {
                return;
            }

            this.is_loading = false;
            if (result === null) {
                this.load_failed = true;
                this.active_request_key = null;
                return;
            }
            this.related_programs = this.deduplicatePrograms(result.recorded_programs);
            this.total = result.total;
            this.fetched_pages = 1;
            await this.scrollCurrentProgramIntoView();
        },

        /** 現在の検索条件を維持したまま、関連番組の次ページを追加する。 */
        async loadMore(): Promise<void> {
            if (this.has_more === false || this.is_loading_more || this.active_request_key === null) return;

            const program_id = this.playerStore.recorded_program.id;
            const request_key = this.active_request_key;
            const request_sequence = this.request_sequence;
            const page = this.fetched_pages + 1;
            this.is_loading_more = true;
            const result = await Videos.fetchRelatedVideos(
                program_id,
                this.filter_mode,
                this.show_other_channels,
                'desc',
                page,
            );
            if (
                request_sequence !== this.request_sequence ||
                this.playerStore.recorded_program.id !== program_id ||
                this.active_request_key !== request_key
            ) {
                return;
            }

            this.is_loading_more = false;
            if (result === null) return;
            this.related_programs = this.deduplicatePrograms([
                ...this.related_programs,
                ...result.recorded_programs,
            ]);
            this.total = result.total;
            this.fetched_pages = page;
        },

        deduplicatePrograms(programs: IRecordedProgram[]): IRecordedProgram[] {
            const program_ids = new Set<number>();
            return programs.filter((program) => {
                if (program_ids.has(program.id)) return false;
                program_ids.add(program.id);
                return true;
            });
        },

        /** 長いリストでも、タブを開いた時点で現在再生中の番組を見つけられる位置へ移動する。 */
        async scrollCurrentProgramIntoView(): Promise<void> {
            await this.$nextTick();
            if (this.is_series_tab_active === false) return;
            const current_program = (this.$el as HTMLElement).querySelector<HTMLElement>('.series-item--current');
            current_program?.scrollIntoView({block: 'center'});
        },

        isCurrentProgram(program: IRecordedProgram): boolean {
            return program.id === this.playerStore.recorded_program.id;
        },

        episodeNumberLabel(program: IRecordedProgram): string {
            // 正本の構造化話数だけを使い、旧互換 episode_number は参照しない。未確定は何も返さない。
            const episode = program.series_episode;
            if (episode === null || episode === undefined) return '';
            if (episode.season_number === 1) {
                return `第${formatRecordedEpisodeNumber(episode.episode_number)}話`;
            }
            return formatRecordedEpisodeLabel(episode.season_number, episode.episode_number);
        },

        episodeTitle(program: IRecordedProgram): string {
            // 題名は話数に紐づく副題だけを使い、元の番組 title へ fallback しない。無いときは空。
            return extractValidEpisodeSubtitle(program.subtitle, [program.series_title, program.title]) ?? '';
        },

        formatDate(start_time: string): string {
            const start = dayjs(start_time);
            const now = dayjs();
            if (start.isSame(now, 'day')) return `今日 ${start.format('HH:mm')}`;
            if (start.isSame(now.subtract(1, 'day'), 'day')) return `昨日 ${start.format('HH:mm')}`;
            if (start.isSame(now, 'week')) return `${start.format('ddd')} ${start.format('HH:mm')}`;
            return start.format('M/D HH:mm');
        },
    },
});

</script>
<style lang="scss" scoped>

.series-container {
    display: flex;
    flex-direction: column;
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

.series-controls {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    position: sticky;
    top: 0;
    margin-bottom: 12px;
    padding: 12px 0 10px;
    gap: 8px;
    background: rgb(var(--v-theme-background));
    z-index: 2;
    @include tablet-vertical {
        padding-top: 0;
    }

    &__toggle {
        flex-shrink: 0;
    }

    &__checkbox {
        flex-shrink: 0;
        margin-left: auto;
    }

    &__series-link {
        flex-shrink: 0;
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

    &__description {
        max-width: 280px;
        margin-top: -6px;
        font-size: 11.5px;
        line-height: 1.6;
    }
}

.series-info {
    margin-bottom: 10px;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 12px;
    font-weight: 600;
}

.series-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.series-item {
    display: flex;
    flex-direction: column;
    padding: 10px;
    gap: 8px;
    border-radius: 6px;
    color: rgb(var(--v-theme-text));
    background: rgb(var(--v-theme-background-lighten-1));
    text-decoration: none;
    transition: background-color 0.15s, transform 0.15s;
    user-select: none;
    @include smartphone-vertical {
        padding: 8px;
        gap: 7px;
    }

    &:hover {
        background: rgb(var(--v-theme-background-lighten-2));
        transform: translateX(2px);
    }

    &--current {
        padding-left: 7px;
        border-left: 3px solid rgb(var(--v-theme-primary));
        background: rgba(var(--v-theme-primary), 0.13);
    }

    &__thumbnail {
        position: relative;
        width: 100%;
        height: auto;
        aspect-ratio: 16 / 9;
        border-radius: 4px;
        background: rgb(var(--v-theme-background));
        overflow: hidden;
    }

    &__thumbnail-image {
        display: block;
        width: 100%;
        height: 100%;
        object-fit: cover;
    }

    &__thumbnail-duration {
        position: absolute;
        right: 4px;
        bottom: 4px;
        padding: 2px 4px;
        border-radius: 2px;
        color: #fff;
        background: rgba(0, 0, 0, 0.75);
        font-size: 10px;
        line-height: 1.2;
    }

    &__thumbnail-status {
        display: flex;
        align-items: center;
        position: absolute;
        top: 4px;
        left: 4px;
        padding: 3px 6px;
        gap: 3px;
        border-radius: 3px;
        color: #fff;
        background: rgba(244, 67, 54, 0.9);
        font-size: 10px;
        font-weight: 600;
        line-height: 1;
    }

    &__thumbnail-status-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: #fff;
    }

    &__heading {
        display: flex;
        align-items: flex-start;
        min-width: 0;
        gap: 7px;
    }

    &__episode {
        align-self: flex-start;
        padding: 2px 5px;
        border-radius: 3px;
        color: rgb(var(--v-theme-primary));
        background: rgba(var(--v-theme-primary), 0.13);
        font-size: 10.5px;
        font-weight: bold;
        line-height: 1.5;
        white-space: nowrap;
    }

    &__title {
        display: -webkit-box;
        min-width: 0;
        overflow: hidden;
        font-size: 13.5px;
        font-weight: 600;
        line-height: 1.5;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 3;
        line-clamp: 3;
        overflow-wrap: anywhere;
        @include smartphone-vertical {
            font-size: 13px;
            -webkit-line-clamp: 2;
            line-clamp: 2;
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
        justify-content: space-between;
        min-width: 0;
        gap: 8px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 11.5px;
    }

    &__channel {
        display: flex;
        align-items: center;
        min-width: 0;
        flex-grow: 1;
        gap: 6px;

        span {
            overflow: hidden;
            white-space: nowrap;
            text-overflow: ellipsis;
        }
    }

    &__channel-logo {
        flex-shrink: 0;
        width: 32px;
        height: 18px;
        border-radius: 2px;
        background: rgb(var(--v-theme-background-lighten-2));
        overflow: hidden;

        img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }
    }

    &__date {
        flex-shrink: 0;
        white-space: nowrap;
    }
}

.series-load-more {
    display: flex;
    justify-content: center;
    margin-top: 14px;
}

</style>
