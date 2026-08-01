
import * as Comlink from 'comlink';

import {
    ICaptureCompositorConstructor,
    ICaptureCompositorOptions,
} from '@/workers/CaptureCompositor';


// CaptureCompositor を Web Worker 上で動作させるためのラッパー
// Comlink を経由し、Web Worker とメインスレッド間でオブジェクトをやり取りする
// Worker 側は Comlink.expose() を明示しているため、vite-plugin-comlink の ComlinkWorker は使わない。
// 両方を併用すると expose が二重登録され、非同期 static method より空 module API のエラー応答が先着する。
let capture_compositor_proxy: Comlink.Remote<ICaptureCompositorConstructor> | null = null;

function getCaptureCompositorProxy(): Comlink.Remote<ICaptureCompositorConstructor> {
    if (capture_compositor_proxy === null) {
        capture_compositor_proxy = Comlink.wrap<ICaptureCompositorConstructor>(
            new Worker(new URL('./CaptureCompositor', import.meta.url), {type: 'module'}),
        );
    }
    return capture_compositor_proxy;
}

// Worker は最初の利用時に生成する。これにより、PlayerController のロジックだけを検証する
// jsdom 環境でもモジュール import 時に Worker API を要求しない。
const LazyCaptureCompositorProxy = function(options: ICaptureCompositorOptions) {
    return new (getCaptureCompositorProxy())(options);
};
LazyCaptureCompositorProxy.loadFonts = (): Promise<void> => getCaptureCompositorProxy().loadFonts();

const CaptureCompositorProxy =
    LazyCaptureCompositorProxy as unknown as Comlink.Remote<ICaptureCompositorConstructor>;
export default CaptureCompositorProxy;
