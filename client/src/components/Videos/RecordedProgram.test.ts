import { mount } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { describe, expect, it } from 'vitest';

import type { IOfflineDownloadJob } from '@/services/OfflineVideos';

import RecordedProgram from '@/components/Videos/RecordedProgram.vue';
import { IRecordedProgramDefault, type IRecordedProgram } from '@/services/Videos';

const program: IRecordedProgram = {
    ...IRecordedProgramDefault,
    id: 177,
    title: 'テスト番組',
    description: '通常の番組説明',
    duration: 60,
    recorded_video: {
        ...IRecordedProgramDefault.recorded_video,
        id: 177,
        status: 'Recorded',
        duration: 60,
    },
};

const createOfflineJob = (
    state: IOfflineDownloadJob['state'],
    phase: IOfflineDownloadJob['phase'],
): IOfflineDownloadJob => ({
    job_id: 'test-job',
    video_id: program.id,
    generation_id: 'test-generation',
    program,
    quality: '240p',
    video_codec: 'av1',
    video_bit_depth: 10,
    requested_audio_codec: 'opus',
    state,
    phase,
    progress: 0.42,
    estimated_size_bytes: 1000,
    downloaded_bytes: 420,
    total_assets: 10,
    package_size_bytes: null,
    server_job_id: 'server-job',
    background_fetch_id: null,
    error: state === 'Failed' ? 'テスト用の保存失敗結果' : null,
});

const mountProgram = (offlineDownloadJob: IOfflineDownloadJob, forOffline: boolean) => {
    return mount(RecordedProgram, {
        props: {program, offlineDownloadJob, forOffline},
        global: {
            plugins: [createPinia()],
            directives: {ripple: {}, ftooltip: {}},
            stubs: {
                RouterLink: {template: '<div><slot /></div>'},
                Icon: true,
                OfflineVideoDownloadDialog: true,
                RecordedFileInfoDialog: true,
                'v-alert': true,
                'v-btn': true,
                'v-card': true,
                'v-card-actions': true,
                'v-card-text': true,
                'v-card-title': true,
                'v-chip': true,
                'v-dialog': true,
                'v-divider': true,
                'v-list': true,
                'v-list-item': true,
                'v-list-item-title': true,
                'v-menu': true,
                'v-spacer': true,
            },
        },
    });
};

describe('RecordedProgram のオフライン保存状態表示', () => {

    it('通常の録画一覧ではオフライン保存の進捗・失敗結果を表示しない', () => {
        const progress_wrapper = mountProgram(createOfflineJob('Downloading', 'Encoding'), false);

        expect(progress_wrapper.text()).not.toContain('映像・音声生成中');
        expect(progress_wrapper.find('.recorded-program__offline-progress').exists()).toBe(false);

        const failure_wrapper = mountProgram(createOfflineJob('Failed', 'Packaging'), false);
        expect(failure_wrapper.text()).not.toContain('保存失敗');
        expect(failure_wrapper.text()).not.toContain('テスト用の保存失敗結果');
        expect(failure_wrapper.text()).toContain('通常の番組説明');
    });

    it('オフライン保存一覧では生成進捗と失敗結果を表示する', () => {
        const progress_wrapper = mountProgram(createOfflineJob('Downloading', 'Encoding'), true);

        expect(progress_wrapper.text()).toContain('映像・音声生成中');
        expect(progress_wrapper.find('.recorded-program__offline-progress').exists()).toBe(true);
        expect(progress_wrapper.find('.recorded-program__offline-progress-bar').attributes('style')).toContain('width: 42%');

        const failure_wrapper = mountProgram(createOfflineJob('Failed', 'Packaging'), true);
        expect(failure_wrapper.text()).toContain('保存失敗');
        expect(failure_wrapper.text()).toContain('テスト用の保存失敗結果');
    });
});
