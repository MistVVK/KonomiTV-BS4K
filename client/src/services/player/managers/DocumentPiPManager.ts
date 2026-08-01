
import DPlayer from 'dplayer';
import { watch } from 'vue';
import { bmlBrowserFontNames } from 'web-bml';

import KeyboardShortcutManager from '@/services/player/managers/KeyboardShortcutManager';
import LiveDataBroadcastingManager from '@/services/player/managers/LiveDataBroadcastingManager';
import PlayerManager from '@/services/player/PlayerManager';
import usePlayerStore from '@/stores/PlayerStore';
import useSettingsStore from '@/stores/SettingsStore';


/**
 * Document Picture-in-Picture を管理する PlayerManager
 * 実装簡素化のため、HTMLVideoElement.requestPictureInPicture() による映像のみの Picture-in-Picture 開始処理を乗っ取る形で実装している
 * こうすることで、別途新たにキーボードショートカットやクリックイベントを実装する必要がなくなる
 */
class DocumentPiPManager implements PlayerManager {

    // ユーザー操作により DPlayer 側で画質が切り替わった際、この PlayerManager の再起動が必要かどうかを PlayerController に示す値
    public readonly restart_required_when_quality_switched = false;

    // DPlayer のインスタンス
    // 設計上コンストラクタ以降で変更すべきでないため readonly にしている
    private readonly player: DPlayer;

    // 再生モード (Live: ライブ視聴, Video: ビデオ視聴)
    private readonly playback_mode: 'Live' | 'Video';

    // 視聴画面内のコンテンツ全体の DOM 要素
    private readonly watch_content_element: HTMLDivElement;

    // 視聴画面内のプレイヤーヘッダーの DOM 要素
    // .watch-content の下に .watch-header があることを前提としている
    private readonly watch_header_element: HTMLDivElement;

    // 視聴画面内のプレイヤー全体の DOM 要素
    // .watch-content の下に .watch-player があることを前提としている
    private readonly watch_player_element: HTMLDivElement;

    // ネイティブの HTMLVideoElement.requestPictureInPicture() メソッド
    // Firefox の Document Picture-in-Picture 対応環境では映像単体のネイティブ API が存在しないため、undefined も保持する
    private request_picture_in_picture: (() => Promise<PictureInPictureWindow>) | undefined = undefined;

    // DPlayer がネイティブ Picture-in-Picture 非対応時に非表示にした Picture-in-Picture ボタン
    // Document Picture-in-Picture の入口として再利用し、destroy() で元の表示状態へ戻す
    private pip_button_element: HTMLElement | null = null;

    // DPlayer が登録しなかった Picture-in-Picture ボタンのクリックイベントリスナー
    // ネイティブ Picture-in-Picture 対応時は DPlayer のリスナーと重複しないよう null のままにする
    private pip_button_click_event_listener: (() => void) | null = null;

    // DPlayer が Picture-in-Picture ボタンへ設定した元のインライン display 値
    // destroy() で DPlayer 管理下の表示状態を正確に復元するために保持する
    private pip_button_original_display = '';


    /**
     * コンストラクタ
     * @param player DPlayer のインスタンス
     * @param playback_mode 再生モード (Live: ライブ視聴, Video: ビデオ視聴)
     */
    constructor(player: DPlayer, playback_mode: 'Live' | 'Video') {
        this.player = player;
        this.playback_mode = playback_mode;
        this.watch_content_element = document.querySelector<HTMLDivElement>('.watch-content')!;
        this.watch_header_element = document.querySelector<HTMLDivElement>('.watch-header')!;
        this.watch_player_element = document.querySelector<HTMLDivElement>('.watch-player')!;
    }


    /**
     * Document Picture-in-Picture を開始するイベントハンドラーを登録する
     */
    public async init(): Promise<void> {
        const player_store = usePlayerStore();
        const settings_store = useSettingsStore();

        // Document Picture-in-Picture API がサポートされていない場合は何もしない
        if (('documentPictureInPicture' in window) === false) {
            console.log('[DocumentPiPManager] Initialized. (Document Picture-in-Picture API is not supported.)');
            return;
        }

        // DPlayer 上で Picture-in-Picture が開始された際のイベントを登録
        // HTMLVideoElement.requestPictureInPicture() に上書きしてイベントハンドラーを登録している
        const new_request_picture_in_picture = async () => {

            // すでに Document Picture-in-Picture が開始されている場合は終了
            // この時 Document Picture-in-Picture ウインドウでは pagehide イベントが発火する
            if (documentPictureInPicture.window) {
                documentPictureInPicture.window.close();
                return {} as PictureInPictureWindow;  // 無理やり PictureInPictureWindow 型にキャスト
            }

            // 下記のコードの実装にあたり、以下のリンクを参考にした
            // ref: https://document-picture-in-picture-api.glitch.me/script.js
            // ref: https://developer.chrome.com/docs/web-platform/document-picture-in-picture
            // ref: https://medium.com/@abhishek_guy/guide-to-use-the-document-picture-in-picture-api-51ecfac058f7
            // Chrome 134 以降では、条件を満たす場合、KonomiTV からタブを切り替えた際に自動的に Document Picture-in-Picture が開始される
            // ref: https://developer.chrome.com/blog/automatic-picture-in-picture-media-playback?hl=ja

            // Document Picture-in-Picture ウインドウが表示された時のイベントを登録
            // すでに登録されている場合は上書きされる
            documentPictureInPicture.onenter = (event) => {
                console.log('[DocumentPiPManager] Picture-in-Picture window entered.');
                player_store.is_document_pip = true;
                // コントロール表示タイマーをリセット
                player_store.event_emitter.emit('SetControlDisplayTimer', {
                    is_player_region_event: true,
                    timeout_seconds: 0,
                });
            };

            // Document Picture-in-Picture の開始をリクエスト
            // ここで指定する幅・高さはあくまで初期値で、ユーザーが手動でリサイズした後はリサイズ後の値が利用される
            const pip_window = await documentPictureInPicture.requestWindow({
                width: 540,
                height: 304,
            });

            // requestWindow() が返った直後から外部操作で閉じられ得るため、DOM 準備より先に
            // pagehide を所有する。setup途中で閉じても watch-player を閉じたDocumentへ移さない。
            let pip_cleanup_started = false;
            let stop_theme_watcher: (() => void) | null = null;
            let stop_control_display_watcher: (() => void) | null = null;
            let keyboard_shortcut_manager: KeyboardShortcutManager | null = null;
            let playing_in_pip_container: HTMLDivElement | null = null;
            const cleanup_pip_window = (): void => {
                if (pip_cleanup_started === true) return;
                pip_cleanup_started = true;
                player_store.is_document_pip = false;
                stop_control_display_watcher?.();
                stop_theme_watcher?.();
                void keyboard_shortcut_manager?.destroy();
                playing_in_pip_container?.remove();
                // setupのどの段階で閉じても、所有DOMを必ずメインウインドウへ戻す。
                this.watch_content_element.append(this.watch_header_element);
                this.watch_content_element.append(this.watch_player_element);
                console.log('[DocumentPiPManager] Picture-in-Picture window exited.');
            };
            // TypeScript の同期制御フローによる false 固定を避け、pagehide から更新される状態を毎回読み直す。
            const is_pip_window_closed = (): boolean => {
                return pip_cleanup_started === true || pip_window.closed === true;
            };
            pip_window.onpagehide = cleanup_pip_window;
            if (is_pip_window_closed()) {
                cleanup_pip_window();
                return {} as PictureInPictureWindow;
            }

            // Dark Reader 拡張機能を使っている場合、何故か Document Picture-in-Picture ウインドウでは
            // サイトごとの無効化設定に関わらずダークモード CSS が追加されてしまうため、Dark Reader 自体を無効化する
            // ref: https://github.com/darkreader/darkreader/blob/main/CONTRIBUTING.md#disabling-dark-reader-on-your-site
            const lock = pip_window.document.createElement('meta');
            lock.name = 'darkreader-lock';
            pip_window.document.head.appendChild(lock);
            if (is_pip_window_closed()) {
                cleanup_pip_window();
                return {} as PictureInPictureWindow;
            }

            // すべてのスタイルシートを Document Picture-in-Picture ウインドウにコピー
            // 以前の仕様には copyStyleSheets オプションがあったが、議論の末に削除されてしまったらしい
            [...document.styleSheets].forEach((style_sheet) => {
                try {
                    const cssRules = [...style_sheet.cssRules].map((rule) => rule.cssText).join('');
                    const style = document.createElement('style');
                    style.textContent = cssRules;
                    pip_window.document.head.appendChild(style);
                } catch (e) {
                    const link = document.createElement('link');
                    link.rel = 'stylesheet';
                    link.type = style_sheet.type;
                    link.media = style_sheet.media.mediaText;
                    link.href = style_sheet.href!;
                    pip_window.document.head.appendChild(link);
                }
            });

            // データ放送用のフォントを読み込む
            // web-bml 側でロードしたフォントはメインウインドウにしか存在しないため、別途ロードする必要がある
            const round_gothic_font = new FontFace(bmlBrowserFontNames.roundGothic, LiveDataBroadcastingManager.ROUND_GOTHIC.source);
            round_gothic_font.load();
            pip_window.document.fonts.add(round_gothic_font);
            const square_gothic_font = new FontFace(bmlBrowserFontNames.squareGothic, LiveDataBroadcastingManager.SQUARE_GOTHIC.source);
            square_gothic_font.load();
            pip_window.document.fonts.add(square_gothic_font);

            // body 要素を .watch-container に見立ててクラスを追加
            pip_window.document.body.classList.add('watch-container');
            pip_window.document.body.classList.add('watch-container--fullscreen');

            // コピー済みの Vuetify テーマ定義から現在のテーマを選び、PiP を開いたままの設定変更にも追従する
            const apply_theme = (theme_name: string): void => {
                for (const class_name of [...pip_window.document.body.classList]) {
                    if (class_name.startsWith('v-theme--')) {
                        pip_window.document.body.classList.remove(class_name);
                    }
                }
                pip_window.document.body.classList.add(`v-theme--${theme_name}`);
            };
            apply_theme(settings_store.settings.ui_theme);
            stop_theme_watcher = watch(() => settings_store.settings.ui_theme, apply_theme);

            // player_store.is_control_display が変更された時に .watch-container--control-display クラスを追加・削除する
            stop_control_display_watcher = watch(() => player_store.is_control_display, (value) => {
                if (value) {
                    pip_window.document.body.classList.add('watch-container--control-display');
                } else {
                    pip_window.document.body.classList.remove('watch-container--control-display');
                }
            });

            // Document Picture-in-Picture ウインドウに DOM 要素を移動
            if (is_pip_window_closed()) {
                cleanup_pip_window();
                return {} as PictureInPictureWindow;
            }
            const watch_content = pip_window.document.createElement('div');
            watch_content.classList.add('watch-content');
            watch_content.style.height = '100vh';  // ここで 100vh を指定しないと、Chrome 131 以降データ放送表示時に高さが 0px になり PiP ウインドウが真っ黒になる
            watch_content.append(this.watch_header_element);
            watch_content.append(this.watch_player_element);
            pip_window.document.body.append(watch_content);

            // メインウインドウ側に「ピクチャー イン ピクチャーを再生しています」というテキストを追加
            playing_in_pip_container = document.createElement('div');
            playing_in_pip_container.classList.add('playing-in-pip');
            const playing_in_pip_text = document.createElement('span');
            playing_in_pip_text.classList.add('playing-in-pip__text');
            playing_in_pip_text.textContent = 'Picture-in-Picture を再生しています';
            playing_in_pip_container.append(playing_in_pip_text);
            const playing_in_pip_close_button = document.createElement('button');
            playing_in_pip_close_button.classList.add('playing-in-pip__close-button');
            playing_in_pip_close_button.textContent = 'Picture-in-Picture を終了';
            playing_in_pip_close_button.onclick = () => pip_window.close();
            playing_in_pip_container.append(playing_in_pip_close_button);
            this.watch_content_element.append(playing_in_pip_container);

            // Document Picture-in-Picture ウインドウにマウスカーソルが入った際のイベントを登録
            // コントロール UI の表示状態に影響する
            const event_listener = (event: Event) => {
                player_store.event_emitter.emit('SetControlDisplayTimer', {
                    event: event,
                    is_player_region_event: true,
                    // Document Picture-in-Picture ウインドウではコントロール UI を非表示にするまでの秒数を短くする
                    timeout_seconds: 1.25,
                });
            };
            watch_content.addEventListener('mousemove', event_listener);
            watch_content.addEventListener('touchmove', event_listener);
            watch_content.addEventListener('click', event_listener);

            // キーボードショートカットを登録
            // 通常 KeyboardShortcutManager は PlayerController で管理されるが、この Document Picture-in-Picture ウインドウには
            // キーボードショートカットイベントが登録されておらず、さらにウインドウが閉じられればウインドウ内に登録したイベントも全削除されるため、
            // 別途このウインドウにおいてキーボードショートカットを管理する KeyboardShortcutManager を生成・初期化している
            // 第3引数に Document Picture-in-Picture ウインドウの Document オブジェクトを渡しているのがポイント
            keyboard_shortcut_manager = new KeyboardShortcutManager(this.player, this.playback_mode, pip_window.document);
            keyboard_shortcut_manager.init();  // 完了を待たない

            return {} as PictureInPictureWindow;  // 無理やり PictureInPictureWindow 型にキャスト
        };

        // オリジナルのブラウザネイティブ実装の HTMLVideoElement.requestPictureInPicture() メソッドを退避
        this.request_picture_in_picture = this.player.video.requestPictureInPicture;
        // 独自のフックで上書きする
        this.player.video.requestPictureInPicture = new_request_picture_in_picture;

        // Firefox のように Document Picture-in-Picture API には対応していても映像単体のネイティブ
        // Picture-in-Picture API には対応していないブラウザでは、DPlayer がボタンを非表示にした上で
        // クリックイベントも登録しない。その場合に限ってボタンを表示し、Document Picture-in-Picture の入口を補う。
        // ネイティブ API 対応時は DPlayer の既存クリックイベントをそのまま利用し、二重起動を防ぐ。
        if (document.pictureInPictureEnabled !== true) {
            this.pip_button_element = this.player.container.querySelector<HTMLElement>('.dplayer-pip-icon');
            if (this.pip_button_element !== null) {
                this.pip_button_original_display = this.pip_button_element.style.display;
                this.pip_button_element.style.removeProperty('display');
                this.pip_button_click_event_listener = () => {
                    this.player.video.requestPictureInPicture().catch((reason) => {
                        console.error(reason);
                        if (this.player.options.lang.includes('ja')) {
                            this.player.notice('Picture-in-Picture を開始できませんでした。', undefined, undefined, '#FF6F6A');
                        } else {
                            this.player.notice('Picture-in-Picture could not be started.', undefined, undefined, '#FF6F6A');
                        }
                    });
                };
                this.pip_button_element.addEventListener('click', this.pip_button_click_event_listener);
            }
        }

        // 画質切り替え後に新しい映像要素が生成されるため、画質切り替え後に再度フックする
        this.player.on('quality_end', () => {
            if (!this.player || !this.player.video) {
                return;
            }
            // 画質切り替え後の this.player.video は切り替え前の this.player.video とは
            // 異なる HTMLVideoElement のインスタンスとなるため、再度オリジナルの関数を退避した上で、独自のフックを適用する
            this.request_picture_in_picture = this.player.video.requestPictureInPicture;
            this.player.video.requestPictureInPicture = new_request_picture_in_picture;
        });

        console.log('[DocumentPiPManager] Initialized.');
    }


    /**
     * Document Picture-in-Picture を開始するイベントハンドラーを削除する
     */
    public async destroy(): Promise<void> {

        // Document Picture-in-Picture API がサポートされていない場合は何もしない
        if (('documentPictureInPicture' in window) === false) {
            console.log('[DocumentPiPManager] Destroyed. (Document Picture-in-Picture API is not supported.)');
            return;
        }

        // Document Picture-in-Picture がまだ開始されている場合は終了
        if (documentPictureInPicture.window !== null) {
            documentPictureInPicture.window.close();
        }

        // Document Picture-in-Picture ウインドウが表示された時のイベントを削除
        documentPictureInPicture.onenter = null;

        // DPlayer が登録しなかった Picture-in-Picture ボタンのクリックイベントを削除し、
        // Manager 初期化前のインライン display 値へ戻す
        if (this.pip_button_element !== null && this.pip_button_click_event_listener !== null) {
            this.pip_button_element.removeEventListener('click', this.pip_button_click_event_listener);
            this.pip_button_element.style.display = this.pip_button_original_display;
            this.pip_button_element = null;
            this.pip_button_click_event_listener = null;
        }

        // DPlayer 上で Picture-in-Picture が開始された際のイベントを削除
        if (this.request_picture_in_picture !== undefined) {
            this.player.video.requestPictureInPicture = this.request_picture_in_picture;  // 元のメソッドに戻す
        } else {
            // Firefox では元々 requestPictureInPicture() が存在しないため、init() で追加した own property 自体を削除する
            Reflect.deleteProperty(this.player.video, 'requestPictureInPicture');
        }

        console.log('[DocumentPiPManager] Destroyed.');
    }
}

export default DocumentPiPManager;
