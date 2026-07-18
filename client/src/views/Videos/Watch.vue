<template>
    <Watch :playback_mode="'Video'" />
</template>
<script lang="ts">

import { mapStores } from 'pinia';
import { defineComponent } from 'vue';

import Watch from '@/components/Watch/Watch.vue';
import PlayerController from '@/services/player/PlayerController';
import Videos from '@/services/Videos';
import usePlayerStore from '@/stores/PlayerStore';
import useSettingsStore from '@/stores/SettingsStore';

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
    computed: {
        ...mapStores(usePlayerStore, useSettingsStore),
    },
    // 開始時に実行
    created() {

        // 下記以外の視聴画面の開始処理は Watch コンポーネントの方で自動的に行われる

        // 解析失敗表示の再試行ボタンから、同じ初期化処理をやり直せるようにする
        this.playerStore.event_emitter.on('RetryRecordedPlaybackIndex', this.retryRecordedPlaybackIndex);

        // 再生セッションを初期化
        this.init();
    },
    // チャンネル切り替え時に実行
    // コンポーネント（インスタンス）は再利用される
    // ref: https://v3.router.vuejs.org/ja/guide/advanced/navigation-guards.html#%E3%83%AB%E3%83%BC%E3%83%88%E5%8D%98%E4%BD%8D%E3%82%AB%E3%82%99%E3%83%BC%E3%83%88%E3%82%99
    beforeRouteUpdate(to, from, next) {

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

        // 上記以外の視聴画面の終了処理は Watch コンポーネントの方で自動的に行われる
    },
    methods: {

        // 再生セッションを初期化する
        async init() {

            // 前の初期化処理が残っている場合は、別録画へ状態を書き戻す前に中断する
            playback_index_abort_controller?.abort();
            playback_index_abort_controller = new AbortController();
            const abort_controller = playback_index_abort_controller;

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
