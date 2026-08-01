export interface Deferred<T> {
    promise: Promise<T>;
    resolve: (value: T | PromiseLike<T>) => void;
    reject: (reason?: unknown) => void;
}


/** 非同期ライフサイクルの任意の await 地点をテストから停止・再開する。 */
export function createDeferred<T = void>(): Deferred<T> {
    let resolve!: (value: T | PromiseLike<T>) => void;
    let reject!: (reason?: unknown) => void;
    const promise = new Promise<T>((promise_resolve, promise_reject) => {
        resolve = promise_resolve;
        reject = promise_reject;
    });
    return {promise, resolve, reject};
}
