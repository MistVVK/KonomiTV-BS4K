import DPlayer from 'dplayer';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import DocumentPiPManager from '@/services/player/managers/DocumentPiPManager';


interface MockPlayer {
    container: HTMLDivElement;
    video: HTMLVideoElement;
    options: {
        lang: string;
    };
    notice: ReturnType<typeof vi.fn>;
    on: ReturnType<typeof vi.fn>;
}


function createPlayer(): MockPlayer {
    const watch_content = document.createElement('div');
    watch_content.classList.add('watch-content');
    const watch_header = document.createElement('div');
    watch_header.classList.add('watch-header');
    const watch_player = document.createElement('div');
    watch_player.classList.add('watch-player');
    const container = document.createElement('div');
    const video = document.createElement('video');
    const pip_button = document.createElement('button');
    pip_button.classList.add('dplayer-pip-icon');
    container.append(video, pip_button);
    watch_player.append(container);
    watch_content.append(watch_header, watch_player);
    document.body.append(watch_content);

    return {
        container,
        video,
        options: {
            lang: 'ja',
        },
        notice: vi.fn(),
        on: vi.fn(),
    };
}


describe('DocumentPiPManager', () => {
    const picture_in_picture_enabled_descriptor = Object.getOwnPropertyDescriptor(
        document,
        'pictureInPictureEnabled',
    );

    beforeEach(() => {
        setActivePinia(createPinia());
        document.body.replaceChildren();
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
        if (picture_in_picture_enabled_descriptor === undefined) {
            Reflect.deleteProperty(document, 'pictureInPictureEnabled');
        } else {
            Object.defineProperty(document, 'pictureInPictureEnabled', picture_in_picture_enabled_descriptor);
        }
        document.body.replaceChildren();
    });

    it('requestWindow直後に閉じられても所有DOMを閉じたDocumentへ移さない', async () => {
        Object.defineProperty(document, 'pictureInPictureEnabled', {
            configurable: true,
            value: true,
        });
        const pip_document = document.implementation.createHTMLDocument('PiP');
        const pip_window_mock = {
            closed: true,
            document: pip_document,
            close: vi.fn(),
            onpagehide: null,
        };
        const pip_window = pip_window_mock as unknown as PictureInPictureWindow;
        const document_pip = {
            window: null as PictureInPictureWindow | null,
            onenter: null,
            requestWindow: vi.fn(async () => {
                document_pip.window = pip_window;
                return pip_window;
            }),
        };
        vi.stubGlobal('documentPictureInPicture', document_pip);

        const player = createPlayer();
        player.video.requestPictureInPicture = vi.fn().mockResolvedValue({} as PictureInPictureWindow);
        const manager = new DocumentPiPManager(player as unknown as DPlayer, 'Live');

        await manager.init();
        await player.video.requestPictureInPicture();

        expect(typeof pip_window_mock.onpagehide).toBe('function');
        expect(document.querySelector('.watch-header')).not.toBeNull();
        expect(document.querySelector('.watch-player')).not.toBeNull();
        expect(pip_document.querySelector('.watch-header')).toBeNull();
        expect(pip_document.querySelector('.watch-player')).toBeNull();
        await manager.destroy();
    });

    it('native PiP 非対応時は非表示ボタンを Document PiP の入口にし、destroy で完全に戻す', async () => {
        Object.defineProperty(document, 'pictureInPictureEnabled', {
            configurable: true,
            value: false,
        });
        const request_window = vi.fn().mockRejectedValue(new Error('expected rejection'));
        vi.stubGlobal('documentPictureInPicture', {
            window: null,
            onenter: null,
            requestWindow: request_window,
        });
        vi.spyOn(console, 'error').mockImplementation(() => {});

        const player = createPlayer();
        const pip_button = player.container.querySelector<HTMLButtonElement>('.dplayer-pip-icon')!;
        pip_button.style.display = 'none';
        Reflect.deleteProperty(player.video, 'requestPictureInPicture');
        const manager = new DocumentPiPManager(player as unknown as DPlayer, 'Live');

        await manager.init();
        expect(pip_button.style.display).toBe('');
        expect(Object.hasOwn(player.video, 'requestPictureInPicture')).toBe(true);

        pip_button.click();
        await vi.waitFor(() => expect(request_window).toHaveBeenCalledTimes(1));
        await vi.waitFor(() => {
            expect(player.notice).toHaveBeenCalledWith(
                'Picture-in-Picture を開始できませんでした。',
                undefined,
                undefined,
                '#FF6F6A',
            );
        });

        await manager.destroy();
        expect(pip_button.style.display).toBe('none');
        expect(Object.hasOwn(player.video, 'requestPictureInPicture')).toBe(false);

        pip_button.click();
        await Promise.resolve();
        expect(request_window).toHaveBeenCalledTimes(1);
    });

    it('native PiP 対応時は DPlayer の既存クリックイベントと重複しない', async () => {
        Object.defineProperty(document, 'pictureInPictureEnabled', {
            configurable: true,
            value: true,
        });
        const request_window = vi.fn().mockRejectedValue(new Error('expected rejection'));
        vi.stubGlobal('documentPictureInPicture', {
            window: null,
            onenter: null,
            requestWindow: request_window,
        });
        vi.spyOn(console, 'error').mockImplementation(() => {});

        const player = createPlayer();
        const pip_button = player.container.querySelector<HTMLButtonElement>('.dplayer-pip-icon')!;
        const native_request_picture_in_picture = vi.fn().mockResolvedValue({} as PictureInPictureWindow);
        player.video.requestPictureInPicture = native_request_picture_in_picture;
        // DPlayer が native PiP 対応時に登録するクリックイベントを再現する
        pip_button.addEventListener('click', () => {
            player.video.requestPictureInPicture().catch(() => {});
        });
        const manager = new DocumentPiPManager(player as unknown as DPlayer, 'Live');

        await manager.init();
        pip_button.click();
        await vi.waitFor(() => expect(request_window).toHaveBeenCalledTimes(1));

        await manager.destroy();
        expect(player.video.requestPictureInPicture).toBe(native_request_picture_in_picture);
    });
});
