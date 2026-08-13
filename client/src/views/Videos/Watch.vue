<template>
    <Watch :playback_mode="'Video'" />
</template>
<script lang="ts">

import { mapStores } from 'pinia';
import { defineComponent, markRaw } from 'vue';

import Watch from '@/components/Watch/Watch.vue';
import OfflineVideos from '@/services/OfflineVideos';
import PlayerController from '@/services/player/PlayerController';
import Series from '@/services/Series';
import Videos from '@/services/Videos';
import usePlayerStore, { type PlayerEvents } from '@/stores/PlayerStore';
import useRecordedSeriesStore from '@/stores/RecordedSeriesStore';
import useSettingsStore from '@/stores/SettingsStore';
import useVersionStore from '@/stores/VersionStore';

export default defineComponent({
    name: 'Video-Watch',
    components: {
        Watch,
    },
    data() {
        return {
            // ended が多重発火しても、Series API とルート遷移を1回だけ実行する。
            is_next_recorded_program_transitioning: false,
            next_recorded_program_request_sequence: 0,
            // このコンポーネントが所有する PlayerController
            player_controller: null as PlayerController | null,
            // 非同期初期化を route 世代ごとに無効化するための単調増加値
            lifecycle_generation: 0,
            // 最初の fetch を含む現在世代の全待機を中断する
            lifecycle_abort_controller: markRaw(new AbortController()),
            // unmount 後に新しい世代を開始しないためのフラグ
            is_component_mounted: true,
        };
    },
    computed: {
        ...mapStores(usePlayerStore, useRecordedSeriesStore, useSettingsStore, useVersionStore),
    },
    // 開始時に実行
    created() {

        // 下記以外の視聴画面の開始処理は Watch コンポーネントの方で自動的に行われる

        // 解析失敗表示の再試行ボタンから、同じ初期化処理をやり直せるようにする
        this.playerStore.event_emitter.on('RetryRecordedPlaybackIndex', this.retryRecordedPlaybackIndex);

        // 録画を自然に完走した場合だけ、シリーズ内の次話へ進む。
        this.playerStore.event_emitter.on('RecordedPlaybackEnded', this.handleRecordedPlaybackEnded);

        // 再生セッションを初期化
        void this.startPlayback(Number(this.$route.params.video_id), this.$route.query.source === 'offline');
    },
    // チャンネル切り替え時に実行
    // コンポーネント（インスタンス）は再利用される
    // ref: https://v3.router.vuejs.org/ja/guide/advanced/navigation-guards.html#%E3%83%AB%E3%83%BC%E3%83%88%E5%8D%98%E4%BD%8D%E3%82%AB%E3%82%99%E3%83%BC%E3%83%88%E3%82%99
    beforeRouteUpdate(to, from, next) {

        // 次話照会中に別ルートへ移動した場合は、戻ってきた古いレスポンスから遷移させない。
        this.invalidateNextRecordedProgramTransition();

        // 前の再生セッションを無効化し、最後の route だけを初期化する
        void this.startPlayback(Number(to.params.video_id), to.query.source === 'offline').catch((error) => {
            console.error('[Videos/Watch] Previous player cleanup failed during route update.', error);
        });

        // 次のルートに置き換え
        next();
    },
    // 終了前に実行
    beforeUnmount() {

        this.is_component_mounted = false;

        // destroy() を実行
        // 別のページへ遷移するため、DPlayer のインスタンスを確実に破棄する
        // さもなければ、ブラウザがリロードされるまでバックグラウンドで永遠に再生され続けてしまう
        void this.destroy().catch((error) => {
            console.error('[Videos/Watch] Player cleanup failed during unmount.', error);
        });
        this.playerStore.event_emitter.off('RetryRecordedPlaybackIndex', this.retryRecordedPlaybackIndex);
        this.playerStore.event_emitter.off('RecordedPlaybackEnded', this.handleRecordedPlaybackEnded);
        this.invalidateNextRecordedProgramTransition();

        // 上記以外の視聴画面の終了処理は Watch コンポーネントの方で自動的に行われる
    },
    methods: {

        /** 指定世代が現在もこのコンポーネントに所有されているかを返す。 */
        isLifecycleActive(generation: number, signal: AbortSignal): boolean {
            return (
                this.is_component_mounted === true &&
                this.lifecycle_generation === generation &&
                signal.aborted === false
            );
        },

        /** route 世代を更新し、旧 controller の回収後に最後の route だけを初期化する。 */
        async startPlayback(video_id: number, is_offline_source: boolean): Promise<void> {
            const generation = ++this.lifecycle_generation;
            this.lifecycle_abort_controller.abort();
            const abort_controller = markRaw(new AbortController());
            this.lifecycle_abort_controller = abort_controller;

            // 進行中 destroy へ後続 route も合流できるよう、完了までは共有参照を保持する。
            // 完了後は対象 instance がまだ共有参照と同一の場合だけ null 化する。
            const previous_controller = this.player_controller;
            if (previous_controller !== null) {
                await previous_controller.destroy();
            }
            if (this.player_controller === previous_controller) {
                this.player_controller = null;
            }
            if (this.isLifecycleActive(generation, abort_controller.signal) === false) return;

            try {
                await this.init(generation, abort_controller, video_id, is_offline_source);
            } catch (error) {
                if (this.isLifecycleActive(generation, abort_controller.signal)) {
                    console.error('[Video-Watch] Failed to initialize playback:', error);
                }
            }
        },

        /** Series API の未完了レスポンスと重複遷移を無効化する。 */
        invalidateNextRecordedProgramTransition(): void {
            this.next_recorded_program_request_sequence += 1;
            this.is_next_recorded_program_transitioning = false;
        },

        /** 録画の自然完走時に、同じシリーズの次話があれば現在の再生画面から遷移する。 */
        async handleRecordedPlaybackEnded(event: PlayerEvents['RecordedPlaybackEnded']): Promise<void> {
            if (this.is_next_recorded_program_transitioning) return;
            // 保存版の次話は端末に存在するとは限らないため、Series API を呼ばず現在の録画で停止する
            if (this.playerStore.is_offline_playback === true) return;

            const ended_program_id = event.recorded_program_id;
            const route_program_id = Number(this.$route.params.video_id);
            if (
                Number.isInteger(route_program_id) === false ||
                route_program_id !== ended_program_id ||
                this.playerStore.recorded_program.id !== ended_program_id
            ) {
                return;
            }

            const series_id = this.playerStore.recorded_program.series_id;
            if (series_id === null) return;

            this.is_next_recorded_program_transitioning = true;
            const request_sequence = ++this.next_recorded_program_request_sequence;
            // 長時間再生中に録画が増えた場合も反映するため、自然完走時点の Series を取得し直す。
            const fetched_series = await Series.fetchSeries(series_id);

            // API 待機中に手動遷移・コンポーネント破棄・別録画への切り替えが起きていれば何もしない。
            if (
                request_sequence !== this.next_recorded_program_request_sequence ||
                Number(this.$route.params.video_id) !== ended_program_id ||
                this.playerStore.recorded_program.id !== ended_program_id ||
                this.playerStore.recorded_program.series_id !== series_id
            ) {
                // より新しい遷移が始まっている場合は、その遷移のフラグを古い応答から消さない。
                if (request_sequence === this.next_recorded_program_request_sequence) {
                    this.is_next_recorded_program_transitioning = false;
                }
                return;
            }

            // 取得失敗時は別の並び順へフォールバックせず、現在の録画で停止する。
            if (fetched_series === null) {
                this.is_next_recorded_program_transitioning = false;
                return;
            }
            this.recordedSeriesStore.setSeries(fetched_series);
            const ordered_programs = this.recordedSeriesStore.getOrderedPrograms(
                series_id,
                this.settingsStore.settings.video_series_sort_key,
                this.settingsStore.settings.video_series_sort_direction,
            );
            const current_program_index = ordered_programs.findIndex(program => program.id === ended_program_id);
            const next_program_id = current_program_index >= 0 ?
                ordered_programs[current_program_index + 1]?.id ?? null :
                null;
            if (next_program_id === null || next_program_id === ended_program_id) {
                this.is_next_recorded_program_transitioning = false;
                return;
            }

            // beforeRouteUpdate() がこのリクエスト世代を無効化してから既存 PlayerController を破棄する。
            // push が同一URLなどで不成立だった場合だけ、現在の画面で再度 ended を受け取れる状態へ戻す。
            try {
                await this.$router.push(`/videos/watch/${next_program_id}`);
            } catch (error) {
                // 予期しないルーター例外をイベントハンドラー外へ未処理 Promise として漏らさない。
                console.error('[Video-Watch] Failed to transition to the next recorded program:', error);
            }
            if (
                request_sequence === this.next_recorded_program_request_sequence &&
                Number(this.$route.params.video_id) === ended_program_id
            ) {
                this.is_next_recorded_program_transitioning = false;
            }
        },

        // 再生セッションを初期化する
        async init(
            generation: number,
            abort_controller: AbortController,
            video_id: number,
            is_offline_source: boolean,
        ): Promise<void> {

            // オフライン保存ページからの明示遷移では、通信・能力 API・再生索引 API を呼ばず保存スナップショットを使う。
            // 通常遷移でも番組 API が失敗した場合だけ、完成済み保存世代へフォールバックする。
            let offline_video = is_offline_source ? await OfflineVideos.getVideo(video_id) : null;
            if (is_offline_source === true && offline_video === null) {
                this.$router.push({path: '/not-found/'});
                return;
            }

            // 実況機能の有効状態と録画 codec 能力の encoder を、同じ /version 応答から一度だけ確定する。
            // force=true の連続呼び出しは同一初期化で二重リクエストになるため避ける。
            // フルの /settings/server は管理者専用のため、公開 runtime 情報を使う。
            // 取得に失敗した場合は VersionStore が実況をフェイルクローズで無効として扱う。
            let version_info = offline_video !== null ? true :
                await this.versionStore.fetchServerVersion(true, abort_controller.signal);
            // 通常 URL から開いた場合も、サーバーへ到達できなければ完成済み保存世代へ切り替える
            if (version_info === null && this.isLifecycleActive(generation, abort_controller.signal) === true) {
                offline_video = await OfflineVideos.getVideo(video_id);
                version_info = offline_video !== null ? true : null;
            }
            if (
                version_info === null ||
                this.isLifecycleActive(generation, abort_controller.signal) === false
            ) {
                return;
            }

            // URL 上の録画番組 ID が未定義なら実行しない (フェイルセーフ)
            // 基本あり得ないはずだが、念のため
            if (Number.isInteger(video_id) === false) {
                this.$router.push({path: '/not-found/'});
                return;
            }

            // 録画番組情報を更新する
            let recorded_program = offline_video?.program ?? await Videos.fetchVideo(video_id);
            if (this.isLifecycleActive(generation, abort_controller.signal) === false) return;
            if (recorded_program === null && offline_video === null) {
                offline_video = await OfflineVideos.getVideo(video_id);
                recorded_program = offline_video?.program ?? null;
            }
            if (recorded_program === null) {
                this.$router.push({path: '/not-found/'});
                return;
            }
            this.playerStore.recorded_program = recorded_program;
            this.playerStore.is_offline_playback = offline_video !== null;
            this.playerStore.offline_video = offline_video;

            // 内容変更・手動再解析中は旧索引を起動せず、軽量Metadataが確定するまで待つ。
            // CM判定とサムネイル生成はこの待機条件へ含めない。
            if (offline_video === null && recorded_program.recorded_video.status !== 'Recorded') {
                const metadata_program = await Videos.waitForRecordedMetadata(
                    recorded_program,
                    (program) => {
                        if (this.isLifecycleActive(generation, abort_controller.signal)) {
                            this.playerStore.recorded_program = program;
                        }
                    },
                    abort_controller.signal,
                );
                if (
                    metadata_program === null ||
                    metadata_program.recorded_video.status !== 'Recorded' ||
                    this.isLifecycleActive(generation, abort_controller.signal) === false
                ) {
                    return;
                }
                recorded_program = metadata_program;
                this.playerStore.recorded_program = recorded_program;
            }

            // 現行Versionの索引がない間はメディアプレイヤーを生成せず、専用APIで解析を開始して待つ。
            // これにより長時間のマスタープレイリスト要求とhls.js側のタイムアウトを避ける。
            if (offline_video === null && recorded_program.recorded_video.playback_index_state !== 'Ready') {
                const playback_index = await Videos.waitForRecordedPlaybackIndex(
                    recorded_program.id,
                    (index) => {
                        if (this.isLifecycleActive(generation, abort_controller.signal) === false) return;
                        Object.assign(this.playerStore.recorded_program.recorded_video, {
                            playback_index_status: index.status,
                            playback_index_state: index.state,
                            playback_index_version: index.version,
                            playback_index_current_version: index.current_version,
                            playback_indexed_at: index.indexed_at,
                            playback_index_error_code: index.error_code,
                        });
                        this.playerStore.recorded_playback_index_progress = index.progress;
                        this.playerStore.recorded_playback_index_stage = index.stage;
                    },
                    abort_controller.signal,
                );
                if (
                    playback_index === null ||
                    playback_index.state !== 'Ready' ||
                    this.isLifecycleActive(generation, abort_controller.signal) === false
                ) {
                    return;
                }

                // 索引生成で更新された映像・音声・字幕タイムラインをプレイヤーへ渡すため、番組情報も再取得する。
                const refreshed_program = await Videos.fetchVideo(recorded_program.id);
                if (
                    refreshed_program === null ||
                    this.isLifecycleActive(generation, abort_controller.signal) === false
                ) return;
                this.playerStore.recorded_program = refreshed_program;
            }

            // PlayerController のコンストラクタには非同期処理を開始する責務はないが、
            // 将来の変更でも旧世代の instance を生成しないよう、生成直前にも所有世代を確認する。
            if (this.isLifecycleActive(generation, abort_controller.signal) === false) return;

            // PlayerController を初期化
            const controller = markRaw(new PlayerController('Video', abort_controller.signal));
            if (this.isLifecycleActive(generation, abort_controller.signal) === false) {
                // 生成後に同期的な再入などで世代が変わった場合も、View の共有参照へ載せる前に必ず回収する。
                await controller.destroy();
                return;
            }
            this.player_controller = controller;
            await controller.init();
            if (this.isLifecycleActive(generation, abort_controller.signal) === false) {
                await controller.destroy();
                if (this.player_controller === controller) {
                    this.player_controller = null;
                }
            }
        },

        // 再生セッションを破棄する
        // 再生する録画番組を切り替える際にも実行される
        async destroy() {
            ++this.lifecycle_generation;
            this.lifecycle_abort_controller.abort();

            // PlayerController を破棄
            const controller = this.player_controller;
            if (controller !== null) {
                await controller.destroy();
            }
            if (this.player_controller === controller) {
                this.player_controller = null;
            }
        },

        // 解析失敗後に同じ録画の索引生成を再要求する
        async retryRecordedPlaybackIndex() {
            if (this.playerStore.is_offline_playback === true) return;
            const generation = this.lifecycle_generation;
            const signal = this.lifecycle_abort_controller.signal;
            if (this.playerStore.recorded_program.recorded_video.status === 'AnalysisFailed') {
                this.playerStore.recorded_program.recorded_video.status = 'Analyzing';
                const succeeded = await Videos.reanalyzeVideo(this.playerStore.recorded_program.id);
                if (this.isLifecycleActive(generation, signal) === false) return;
                if (succeeded === false) {
                    this.playerStore.recorded_program.recorded_video.status = 'AnalysisFailed';
                    return;
                }
            }
            void this.startPlayback(Number(this.$route.params.video_id), this.$route.query.source === 'offline');
        }
    }
});

</script>
