<template>
    <Watch :playback_mode="'Live'" />
</template>
<script lang="ts">

import { mapStores } from 'pinia';
import { defineComponent, markRaw } from 'vue';

import Watch from '@/components/Watch/Watch.vue';
import PlayerController from '@/services/player/PlayerController';
import useChannelsStore from '@/stores/ChannelsStore';
import usePlayerStore from '@/stores/PlayerStore';
import useSettingsStore from '@/stores/SettingsStore';
import useVersionStore from '@/stores/VersionStore';
import Utils, { dayjs } from '@/utils';

export default defineComponent({
    name: 'TV-Watch',
    components: {
        Watch,
    },
    data() {
        return {
            // インターバル ID
            // ページ遷移時に setInterval(), setTimeout() の実行を止めるのに使う
            // setInterval(), setTimeout() の返り値を登録する
            interval_ids: [] as number[],
            // このコンポーネントが所有する PlayerController
            // markRaw() で Vue の再帰的なリアクティブ化を避ける
            player_controller: null as PlayerController | null,
            // 非同期初期化を route 世代ごとに無効化するための単調増加値
            lifecycle_generation: 0,
            // 現在の route 世代に属する API 待機・タイマー待機を中断する
            lifecycle_abort_controller: markRaw(new AbortController()),
            // unmount 後に新しい世代を開始しないためのフラグ
            is_component_mounted: true,
        };
    },
    computed: {
        ...mapStores(useChannelsStore, usePlayerStore, useSettingsStore, useVersionStore),
    },
    // 開始時に実行
    created() {

        // 下記以外の視聴画面の開始処理は Watch コンポーネントの方で自動的に行われる

        // 再生セッションを初期化
        void this.startPlayback(this.$route.params.display_channel_id as string);
    },
    // チャンネル切り替え時に実行
    // コンポーネント（インスタンス）は再利用される
    // ref: https://v3.router.vuejs.org/ja/guide/advanced/navigation-guards.html#%E3%83%AB%E3%83%BC%E3%83%88%E5%8D%98%E4%BD%8D%E3%82%AB%E3%82%99%E3%83%BC%E3%83%88%E3%82%99
    beforeRouteUpdate(to, from, next) {

        // このコンポーネントはチャンネル切り替え時に再利用されるため、前チャンネルの添付選択を明示的に解除する
        this.playerStore.clearTwitterCaptureSelection();

        // ザッピング時だけ 0.5 秒の猶予を設ける。連続 route update では AbortSignal により
        // 古い待機と初期化が無効化され、最後の route だけが controller を所有する。
        const delay_seconds = this.playerStore.is_zapping === true ? 0.5 : 0;
        this.playerStore.is_zapping = false;
        void this.startPlayback(to.params.display_channel_id as string, delay_seconds).catch((error) => {
            console.error('[TV/Watch] Previous player cleanup failed during route update.', error);
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
            console.error('[TV/Watch] Player cleanup failed during unmount.', error);
        });

        // このページから離れるので、チャンネル ID を gr000 (ダミー値) に戻す
        this.channelsStore.display_channel_id = 'gr000';

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

        /** 登録済みの timer をすべて停止する。 */
        clearTimers(): void {
            for (const interval_id of this.interval_ids) {
                window.clearInterval(interval_id);
            }
            this.interval_ids = [];
        },

        /** route 世代を更新し、旧 controller の回収後に最後の route だけを初期化する。 */
        async startPlayback(display_channel_id: string, delay_seconds: number = 0): Promise<void> {
            const generation = ++this.lifecycle_generation;
            this.lifecycle_abort_controller.abort();
            const abort_controller = markRaw(new AbortController());
            this.lifecycle_abort_controller = abort_controller;
            this.clearTimers();

            // 進行中 destroy へ後続 route も合流できるよう、完了までは共有参照を保持する。
            // 完了後は対象 instance がまだ共有参照と同一の場合だけ null 化する。
            const previous_controller = this.player_controller;
            this.channelsStore.display_channel_id = display_channel_id;
            if (previous_controller !== null) {
                await previous_controller.destroy();
            }
            if (this.player_controller === previous_controller) {
                this.player_controller = null;
            }

            if (delay_seconds > 0) {
                await Utils.sleep(delay_seconds, abort_controller.signal);
            }
            if (this.isLifecycleActive(generation, abort_controller.signal) === false) return;

            try {
                await this.init(generation, abort_controller, display_channel_id);
            } catch (error) {
                if (this.isLifecycleActive(generation, abort_controller.signal)) {
                    console.error('[TV-Watch] Failed to initialize playback:', error);
                }
            }
        },

        // 再生セッションを初期化する
        async init(generation: number, abort_controller: AbortController, display_channel_id: string): Promise<void> {

            // プレイヤー生成前に必要な実況機能の有効状態とチャンネル情報は相互に依存しないため、並列に取得する。
            // 直列に待つと選局 URL への遷移からライブ接続開始までに 2 回分の API 待ち時間が積み上がり、
            // エンコーダーが ONAir になってから初画までの遅延であるかのように見えてしまう。
            // どちらの Store も同じ AbortSignal で旧 route からの更新を拒否し、VersionStore は取得失敗時に
            // 実況機能をフェイルクローズで無効として扱うため、並列化しても従来の安全側の挙動は維持される。
            await Promise.all([
                this.versionStore.fetchServerVersion(true, abort_controller.signal),
                this.channelsStore.update(false, abort_controller.signal),
            ]);
            if (this.isLifecycleActive(generation, abort_controller.signal) === false) return;

            // 00秒までの残り秒数を取得
            // 現在 16:01:34 なら 26 (秒) になる
            const residue_second = 60 - dayjs().second();

            // 00秒になるまで待ってから実行するタイマー
            // 番組は基本1分単位で組まれているため、20秒や45秒など中途半端な秒数で更新してしまうと番組情報の反映が遅れてしまう
            this.interval_ids.push(window.setTimeout(() => {
                if (this.isLifecycleActive(generation, abort_controller.signal) === false) return;

                // この時点で00秒なので、チャンネル情報を更新
                this.channelsStore.update(true, abort_controller.signal);

                // 以降、30秒おきにチャンネル情報を更新
                this.interval_ids.push(window.setInterval(() => {
                    if (this.isLifecycleActive(generation, abort_controller.signal) === false) return;
                    this.channelsStore.update(true, abort_controller.signal);
                }, 30 * 1000));

            }, residue_second * 1000));

            // URL 上のチャンネル ID が未定義なら実行しない (フェイルセーフ)
            // 基本あり得ないはずだが、念のため
            if (display_channel_id === undefined) {
                this.$router.push({path: '/not-found/'});
                return;
            }

            // もしこの時点でチャンネル名が「チャンネル情報取得エラー」の場合、
            // URL で指定された display_channel_id に紐づくチャンネル情報がないことを示しているので、404 ページにリダイレクト
            if (this.channelsStore.channel.current.name === 'チャンネル情報取得エラー') {
                await Utils.sleep(3, abort_controller.signal);  // 3秒待機
                if (this.isLifecycleActive(generation, abort_controller.signal) === false) return;
                this.$router.push({path: '/not-found/'});
                return;
            }

            // PlayerController のコンストラクタには非同期処理を開始する責務はないが、
            // 将来の変更でも旧世代の instance を生成しないよう、生成直前にも所有世代を確認する。
            if (this.isLifecycleActive(generation, abort_controller.signal) === false) return;

            // PlayerController を初期化
            const controller = markRaw(new PlayerController('Live', abort_controller.signal));
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
        // チャンネルを切り替える際に実行される
        async destroy() {
            ++this.lifecycle_generation;
            this.lifecycle_abort_controller.abort();
            this.clearTimers();

            // PlayerController を破棄
            const controller = this.player_controller;
            if (controller !== null) {
                await controller.destroy();
            }
            if (this.player_controller === controller) {
                this.player_controller = null;
            }
        }
    }
});

</script>
