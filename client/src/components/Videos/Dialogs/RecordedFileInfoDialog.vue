<template>
    <v-dialog max-width="770" transition="slide-y-transition" :model-value="show" @update:model-value="$emit('update:show', $event)">
        <v-card class="video-info">
            <v-card-title class="px-5 pt-6 pb-3 d-flex align-center font-weight-bold" style="height: 60px;">
                <Icon icon="fluent:document-20-filled" height="26px" />
                <span class="ml-3">録画ファイル情報</span>
                <v-spacer></v-spacer>
                <div v-ripple class="d-flex align-center rounded-circle cursor-pointer px-2 py-2" @click="$emit('update:show', false)">
                    <Icon icon="fluent:dismiss-12-filled" width="23px" height="23px" />
                </div>
            </v-card-title>
            <div class="px-5 pb-6">
                <div class="text-subtitle-1 d-flex align-center font-weight-bold mt-2">
                    <Icon icon="fluent:video-clip-20-filled" width="24px" height="20px" />
                    <span class="ml-2">ファイル情報</span>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">ファイルパス</div>
                    <div class="video-info__item-value">{{program.recorded_video.file_path}}</div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">ファイルサイズ</div>
                    <div class="video-info__item-value">{{Utils.formatBytes(program.recorded_video.file_size)}}</div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">録画期間</div>
                    <div class="video-info__item-value">
                        {{ProgramUtils.getRecordingTime(program)}}
                    </div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">最終更新日時</div>
                    <div class="video-info__item-value">
                        {{Utils.apply28HourClock(dayjs(program.recorded_video.file_modified_at).format('YYYY/MM/DD (dd) HH:mm:ss'))}}
                    </div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">解析日時</div>
                    <div class="video-info__item-value">
                        {{program.recorded_video.analyzed_at !== null ? Utils.apply28HourClock(dayjs(program.recorded_video.analyzed_at).format('YYYY/MM/DD (dd) HH:mm:ss')) : '不明'}}
                    </div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">解析時ビルド番号</div>
                    <div class="video-info__item-value">{{program.recorded_video.analysis_git_commit ?? '不明'}}</div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">再生索引状態</div>
                    <div class="video-info__item-value">{{formatPlaybackIndexState(program.recorded_video.playback_index_state)}}</div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">索引バージョン</div>
                    <div class="video-info__item-value">
                        {{program.recorded_video.playback_index_version ?? '未生成'}}
                        （現行: {{program.recorded_video.playback_index_current_version}}）
                    </div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">索引解析日時</div>
                    <div class="video-info__item-value">
                        {{program.recorded_video.playback_indexed_at !== null ?
                            Utils.apply28HourClock(dayjs(program.recorded_video.playback_indexed_at).format('YYYY/MM/DD (dd) HH:mm:ss')) : '未解析'}}
                    </div>
                </div>
                <div v-if="program.recorded_video.playback_index_error_code !== null" class="video-info__item">
                    <div class="video-info__item-label">索引解析エラー</div>
                    <div class="video-info__item-value">{{program.recorded_video.playback_index_error_code}}</div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">CM情報</div>
                    <div class="video-info__item-value">
                        {{formatCMAnalysisStatus()}}
                    </div>
                </div>
                <div v-if="program.recorded_video.cm_result_source !== null" class="video-info__item">
                    <div class="video-info__item-label">CM公開結果</div>
                    <div class="video-info__item-value">{{formatCMResultSource()}}</div>
                </div>
                <div v-if="program.recorded_video.cm_result_published_at !== null" class="video-info__item">
                    <div class="video-info__item-label">CM結果更新日時</div>
                    <div class="video-info__item-value">
                        {{Utils.apply28HourClock(dayjs(program.recorded_video.cm_result_published_at).format('YYYY/MM/DD (dd) HH:mm:ss'))}}
                    </div>
                </div>
                <div class="text-subtitle-1 d-flex align-center font-weight-bold mt-3">
                    <Icon icon="fluent:video-20-filled" width="24px" height="20px" />
                    <span class="ml-2">映像情報</span>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">コーデック</div>
                    <div class="video-info__item-value">{{program.recorded_video.video_codec}} ({{program.recorded_video.video_codec_profile}})</div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">解像度</div>
                    <div class="video-info__item-value">{{program.recorded_video.video_resolution_width}}×{{program.recorded_video.video_resolution_height}}</div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">SAR</div>
                    <div class="video-info__item-value">{{program.recorded_video.video_sample_aspect_ratio ?? '不明'}}</div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">DAR</div>
                    <div class="video-info__item-value">{{program.recorded_video.video_display_aspect_ratio ?? '不明'}}</div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">フレームレート</div>
                    <div class="video-info__item-value">{{program.recorded_video.video_frame_rate}} fps</div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">スキャン方式</div>
                    <div class="video-info__item-value">{{program.recorded_video.video_scan_type}}</div>
                </div>
                <div class="text-subtitle-1 d-flex align-center font-weight-bold mt-3">
                    <Icon icon="fluent:speaker-2-20-filled" width="24px" height="20px" />
                    <span class="ml-2">音声情報（主音声）</span>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">コーデック</div>
                    <div class="video-info__item-value">{{program.recorded_video.primary_audio_codec}}</div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">チャンネル</div>
                    <div class="video-info__item-value">{{program.recorded_video.primary_audio_channel}}</div>
                </div>
                <div class="video-info__item">
                    <div class="video-info__item-label">サンプリングレート</div>
                    <div class="video-info__item-value">{{program.recorded_video.primary_audio_sampling_rate ? `${program.recorded_video.primary_audio_sampling_rate / 1000}kHz` : '不明'}}</div>
                </div>

                <div v-if="program.recorded_video.secondary_audio_codec" class="text-subtitle-1 d-flex align-center font-weight-bold mt-3">
                    <Icon icon="fluent:speaker-2-20-filled" width="24px" height="20px" />
                    <span class="ml-2">音声情報（副音声）</span>
                </div>
                <template v-if="program.recorded_video.secondary_audio_codec">
                    <div class="video-info__item">
                        <div class="video-info__item-label">コーデック</div>
                        <div class="video-info__item-value">{{program.recorded_video.secondary_audio_codec}}</div>
                    </div>
                    <div class="video-info__item">
                        <div class="video-info__item-label">チャンネル</div>
                        <div class="video-info__item-value">{{program.recorded_video.secondary_audio_channel}}</div>
                    </div>
                    <div class="video-info__item">
                        <div class="video-info__item-label">サンプリングレート</div>
                        <div class="video-info__item-value">{{program.recorded_video.secondary_audio_sampling_rate ? `${program.recorded_video.secondary_audio_sampling_rate / 1000}kHz` : '不明'}}</div>
                    </div>
                </template>
                <div class="text-subtitle-1 d-flex align-center font-weight-bold mt-3">
                    <Icon icon="fluent:timeline-20-filled" width="24px" height="20px" />
                    <span class="ml-2">音声構成タイムライン</span>
                </div>
                <div v-if="program.recorded_video.audio_track_timeline.length === 0" class="video-info__item">
                    <div class="video-info__item-label">時間配分</div>
                    <div class="video-info__item-value">不明</div>
                </div>
                <div v-for="(interval, index) in program.recorded_video.audio_track_timeline"
                    v-else :key="`${interval.start_time}-${interval.end_time}-${index}`"
                    class="video-info__item video-info__item--timeline">
                    <div class="video-info__item-label">{{formatTimelineRange(interval.start_time, interval.end_time)}}</div>
                    <div class="video-info__item-value">{{formatTimelineTracks(interval.tracks)}}</div>
                </div>
            </div>
        </v-card>
    </v-dialog>
</template>
<script lang="ts" setup>

import { IAudioTrack, IRecordedProgram } from '@/services/Videos';
import Utils, { ProgramUtils, dayjs } from '@/utils';

// Props
const props = defineProps<{
    program: IRecordedProgram;
    show: boolean;
}>();

// Emits
defineEmits<{
    (e: 'update:show', value: boolean): void;
}>();

/** 録画先頭基準の秒数を HH:MM:SS 形式へ変換する。 */
const formatTimelineTime = (seconds: number): string => {
    const total_seconds = Math.max(0, Math.floor(seconds));
    const hours = Math.floor(total_seconds / 3600);
    const minutes = Math.floor((total_seconds % 3600) / 60);
    const remaining_seconds = total_seconds % 60;
    return [hours, minutes, remaining_seconds].map((value) => String(value).padStart(2, '0')).join(':');
};

/** 音声構成区間の開始・終了と長さを表示する。 */
const formatTimelineRange = (start_time: number, end_time: number): string => {
    return `${formatTimelineTime(start_time)} ～ ${formatTimelineTime(end_time)}` +
        `（${formatTimelineTime(Math.max(0, end_time - start_time))}）`;
};

/** 音声構成区間に実在する論理Trackを表示する。 */
const formatTimelineTracks = (tracks: IAudioTrack[]): string => {
    if (tracks.length === 0) return '音声なし';
    return tracks.map((track) => {
        const language = track.language !== null && track.language.length > 0 ? ` / ${track.language}` : '';
        return `Track ${track.index}: ${track.channel}${language}`;
    }).join('、');
};

/** APIが計算した録画再生索引状態を利用者向けの表示へ変換する。 */
const formatPlaybackIndexState = (state: IRecordedProgram['recorded_video']['playback_index_state']): string => {
    return {
        Pending: '解析待ち',
        Analyzing: '解析中',
        Ready: '再生準備完了',
        Stale: '更新が必要',
        Failed: '解析失敗',
    }[state];
};

/** CM区間数とは独立した専用解析状態を利用者向けの表示へ変換する。 */
const formatCMAnalysisStatus = (): string => {
    const recorded_video = props.program.recorded_video;
    const status = recorded_video.cm_analysis_status;
    if (status === 'Completed') {
        const section_count = recorded_video.cm_sections?.length ?? 0;
        return section_count > 0 ? `解析完了（CM ${section_count}区間）` : '解析完了（CMなし）';
    }
    if (status === null) return '未解析';
    const label = {
        Pending: '解析待ち',
        Analyzing: '解析中',
        Failed: '解析失敗',
        Unsupported: '非対応',
        Excluded: '除外',
        Interrupted: '中断',
    }[status];
    const state = recorded_video.cm_analysis_error_code !== null
        ? `${label}（${recorded_video.cm_analysis_error_code}）`
        : label;
    return recorded_video.cm_result_source !== null ? `${state}・前回の公開結果を維持` : state;
};

/** 最新試行とは独立した、現在再生に使うCM結果の由来を表示する。 */
const formatCMResultSource = (): string => {
    const recorded_video = props.program.recorded_video;
    if (recorded_video.cm_result_source === null) return 'なし';
    const source = {
        Existing: '外部 chapter',
        Generated: 'KonomiTV 自動解析',
        LegacyImported: '旧命名 chapter（互換読込）',
    }[recorded_video.cm_result_source];
    const path_kind = recorded_video.cm_result_chapter_path_kind === 'Legacy' ? ' / 旧命名' : '';
    const pipeline = recorded_video.cm_result_pipeline_version !== null
        ? ` / ${recorded_video.cm_result_pipeline_version}`
        : '';
    const verification = recorded_video.cm_result_verified === false ? ' / 未検証移行データ' : '';
    return `${source}${path_kind}${pipeline}${verification}`;
};

</script>
<style lang="scss" scoped>

.video-info {
    &__item {
        display: flex;
        margin-top: 8px;

        &-label {
            flex-shrink: 0;
            width: 140px;
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 14px;
        }

        &-value {
            flex-grow: 1;
            font-size: 14px;
            word-break: break-word;
        }
    }

    &__item--timeline {
        .video-info__item-label {
            // HH:MM:SSの開始・終了・区間長を折り返さず表示できる幅を確保する
            width: 245px;
            font-variant-numeric: tabular-nums;
        }
    }
}

</style>
