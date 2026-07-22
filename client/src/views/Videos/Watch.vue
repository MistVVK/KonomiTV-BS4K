<template>
    <Watch :playback_mode="'Video'" />
</template>
<script lang="ts">

import { mapStores } from 'pinia';
import { defineComponent } from 'vue';

import Watch from '@/components/Watch/Watch.vue';
import PlayerController from '@/services/player/PlayerController';
import Series from '@/services/Series';
import Videos from '@/services/Videos';
import usePlayerStore, { type PlayerEvents } from '@/stores/PlayerStore';
import useRecordedSeriesStore from '@/stores/RecordedSeriesStore';
import useSettingsStore from '@/stores/SettingsStore';
import useVersionStore from '@/stores/VersionStore';

// PlayerController のインスタンス
// data() 内に記述すると再帰的にリアクティブ化され重くなる上リアクティブにする必要自体がないので、グローバル変数にしている
let player_controller: PlayerController | null = null;

// 録画再生索引の監視は画面遷移時に確実に中断し、前の録画の完了通知を反映させない
let playback_index_abort_controller: AbortController | null = null;

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
        this.init();
    },
    // チャンネル切り替え時に実行
    // コンポーネント（インスタンス）は再利用される
    // ref: https://v3.router.vuejs.org/ja/guide/advanced/navigation-guards.html#%E3%83%AB%E3%83%BC%E3%83%88%E5%8D%98%E4%BD%8D%E3%82%AB%E3%82%99%E3%83%BC%E3%83%88%E3%82%99
    beforeRouteUpdate(to, from, next) {

        // 次話照会中に別ルートへ移動した場合は、戻ってきた古いレスポンスから遷移させない。
        this.invalidateNextRecordedProgramTransition();

        // 前の再生セッションを破棄して終了し、完了を待ってから再度初期化する
        const destroy_promise = this.destroy();
        destroy_promise.then(() => this.init());

        // 次のルートに置き換え
        next();
    },
    // 終了前に実行
    beforeUnmount() {

        // destroy() を実行
        // 別のページへ遷移するため、DPlayer のインスタンスを確実に破棄する
        // さもなければ、ブラウザがリロードされるまでバックグラウンドで永遠に再生され続けてしまう
        this.destroy();
        this.playerStore.event_emitter.off('RetryRecordedPlaybackIndex', this.retryRecordedPlaybackIndex);
        this.playerStore.event_emitter.off('RecordedPlaybackEnded', this.handleRecordedPlaybackEnded);
        this.invalidateNextRecordedProgramTransition();

        // 上記以外の視聴画面の終了処理は Watch コンポーネントの方で自動的に行われる
    },
    methods: {

        /** Series API の未完了レスポンスと重複遷移を無効化する。 */
        invalidateNextRecordedProgramTransition(): void {
            this.next_recorded_program_request_sequence += 1;
            this.is_next_recorded_program_transitioning = false;
        },

        /** 録画の自然完走時に、同じシリーズの次話があれば現在の再生画面から遷移する。 */
        async handleRecordedPlaybackEnded(event: PlayerEvents['RecordedPlaybackEnded']): Promise<void> {
            if (this.is_next_recorded_program_transitioning) return;

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
        async init() {

            // 前の初期化処理が残っている場合は、別録画へ状態を書き戻す前に中断する
            playback_index_abort_controller?.abort();
            playback_index_abort_controller = new AbortController();
            const abort_controller = playback_index_abort_controller;

            // 実況機能のサーバー側有効状態をプレイヤー生成前に確定する
            // 取得に失敗した場合は VersionStore がフェイルクローズで無効として扱う
            await this.versionStore.fetchServerVersion(true);

            // URL 上の録画番組 ID が未定義なら実行しない (フェイルセーフ)
            // 基本あり得ないはずだが、念のため
            if (this.$route.params.video_id === undefined) {
                this.$router.push({path: '/not-found/'});
                return;
            }

            // 録画番組情報を更新する
            let recorded_program = await Videos.fetchVideo(parseFloat(this.$route.params.video_id as string));
            if (recorded_program === null) {
                this.$router.push({path: '/not-found/'});
                return;
            }
            this.playerStore.recorded_program = recorded_program;

            // 内容変更・手動再解析中は旧索引を起動せず、軽量Metadataが確定するまで待つ。
            // CM判定とサムネイル生成はこの待機条件へ含めない。
            if (recorded_program.recorded_video.status !== 'Recorded') {
                const metadata_program = await Videos.waitForRecordedMetadata(
                    recorded_program,
                    (program) => {
                        this.playerStore.recorded_program = program;
                    },
                    abort_controller.signal,
                );
                if (
                    metadata_program === null ||
                    metadata_program.recorded_video.status !== 'Recorded' ||
                    abort_controller.signal.aborted
                ) {
                    return;
                }
                recorded_program = metadata_program;
                this.playerStore.recorded_program = recorded_program;
            }

            // 現行Versionの索引がない間はメディアプレイヤーを生成せず、専用APIで解析を開始して待つ。
            // これにより長時間のマスタープレイリスト要求とhls.js側のタイムアウトを避ける。
            if (recorded_program.recorded_video.playback_index_state !== 'Ready') {
                const playback_index = await Videos.waitForRecordedPlaybackIndex(
                    recorded_program.id,
                    (index) => {
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
                if (playback_index === null || playback_index.state !== 'Ready' || abort_controller.signal.aborted) {
                    return;
                }

                // 索引生成で更新された映像・音声・字幕タイムラインをプレイヤーへ渡すため、番組情報も再取得する。
                const refreshed_program = await Videos.fetchVideo(recorded_program.id);
                if (refreshed_program === null || abort_controller.signal.aborted) return;
                this.playerStore.recorded_program = refreshed_program;
            }

            // PlayerController を初期化
            player_controller = new PlayerController('Video');
            await player_controller.init();
        },

        // 再生セッションを破棄する
        // 再生する録画番組を切り替える際にも実行される
        async destroy() {

            // 索引監視中ならHTTP要求とポーリングタイマーを停止する
            playback_index_abort_controller?.abort();
            playback_index_abort_controller = null;

            // PlayerController を破棄
            if (player_controller !== null) {
                await player_controller.destroy();
                player_controller = null;
            }
        },

        // 解析失敗後に同じ録画の索引生成を再要求する
        async retryRecordedPlaybackIndex() {
            if (this.playerStore.recorded_program.recorded_video.status === 'AnalysisFailed') {
                this.playerStore.recorded_program.recorded_video.status = 'Analyzing';
                const succeeded = await Videos.reanalyzeVideo(this.playerStore.recorded_program.id);
                if (succeeded === false) {
                    this.playerStore.recorded_program.recorded_video.status = 'AnalysisFailed';
                    return;
                }
            }
            void this.init();
        }
    }
});

</script>
