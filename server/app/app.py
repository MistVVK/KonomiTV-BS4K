
import asyncio
import atexit
import mimetypes
from collections.abc import Awaitable
from pathlib import Path

import tortoise.contrib.fastapi
import tortoise.log
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp

from app import logging
from app.CompatibilityAPI import CreateCompatibilityAPI, PortDispatchApplication
from app.config import Config, LoadConfig, ResolveCompatibilityHTTPSSettings
from app.constants import (
    BS4K_VERSION,
    CLIENT_DIR,
    DATABASE_CONFIG,
    QUALITY,
)
from app.metadata.AnalysisTaskTracker import AnalysisTaskTracker
from app.metadata.CMAnalysisOrchestrator import CMAnalysisOrchestrator
from app.metadata.CMAnalysisTaskManager import CMAnalysisTaskManager
from app.metadata.CMAnalysisWorkspace import CMAnalysisWorkspace
from app.metadata.RecordedEpisodeAutomation import RecordedEpisodeAutomation
from app.metadata.RecordedEpisodeResolver import RecordedEpisodeResolver
from app.metadata.RecordedPlaybackIndexer import RecordedPlaybackIndexer
from app.metadata.RecordedScanTask import RecordedScanTask
from app.metadata.RecordedSeriesResolver import RecordedSeriesResolver
from app.models.Channel import Channel
from app.models.NiconicoOAuthState import NiconicoOAuthState
from app.models.Program import Program
from app.models.RefreshToken import RefreshToken
from app.routers import (
    AIBackendRouter,
    AnalysisTasksRouter,
    BlueskyRouter,
    CapturesRouter,
    ChannelsRouter,
    CMAnalysisRouter,
    DataBroadcastingRouter,
    LiveStreamsRouter,
    MaintenanceRouter,
    NiconicoRouter,
    ProgramsRouter,
    RecordedSeriesRouter,
    RecordingPresetsRouter,
    ReservationConditionsRouter,
    ReservationsRouter,
    SeriesRouter,
    SettingsRouter,
    TwitterRouter,
    UsersRouter,
    VersionRouter,
    VideosRouter,
    VideoStreamsRouter,
)
from app.streams.LiveStream import LiveStream
from app.streams.RecordedFMP4Cache import RecordedFMP4CacheManager
from app.streams.RecordedSubtitleStream import RecordedSubtitleStream
from app.utils.edcb.EDCBTuner import EDCBTuner
from app.utils.FastAPITaskUtil import repeat_every
from app.utils.HTTPS import BuildServerStartupSettings, ReverseProxyMiddleware


# もし Config() の実行時に AssertionError が発生した場合は、LoadConfig() を実行してサーバー設定データをロードする
## 自動リロードモードでは app.py がサーバープロセスのエントリーポイントになるため、
## サーバープロセス上にサーバー設定データがロードされていない状態になる
try:
    CONFIG = Config()
except AssertionError:
    # バリデーションは既にサーバー起動時に行われているためスキップする
    CONFIG = LoadConfig(bypass_validation=True)

# FastAPI を初期化
app = FastAPI(
    title = 'KonomiTV-BS4K',
    description = 'KonomiTV-BS4K: Kept Organized, Notably Optimized, Modern Interface TV media server',
    version = BS4K_VERSION,
    openapi_url = '/api/openapi.json',
    docs_url = '/api/docs',
    redoc_url = '/api/redoc',
)


@app.exception_handler(RequestValidationError)
async def RequestValidationExceptionHandler(request: Request, exception: Exception):
    """
    パス設定APIだけ入力値を除いた422へ変換し、それ以外は標準形式を維持する。

    Args:
        request (Request): バリデーションに失敗したHTTPリクエスト。
        exception (Exception): FastAPIが渡すRequestValidationError。

    Returns:
        JSONResponse: SettingsRouterの安全な422レスポンス。
    """

    assert isinstance(exception, RequestValidationError)
    return await SettingsRouter.HostPathRequestValidationErrorHandler(request, exception)

# ルーターの追加
app.include_router(ChannelsRouter.router)
app.include_router(AnalysisTasksRouter.router)
app.include_router(ProgramsRouter.router)
app.include_router(VideosRouter.router)
app.include_router(SeriesRouter.router)
app.include_router(LiveStreamsRouter.router)
app.include_router(VideoStreamsRouter.router)
app.include_router(ReservationsRouter.router)
app.include_router(ReservationConditionsRouter.router)
app.include_router(RecordingPresetsRouter.router)
app.include_router(RecordedSeriesRouter.router)
app.include_router(AIBackendRouter.router)
app.include_router(CapturesRouter.router)
app.include_router(CMAnalysisRouter.router)
app.include_router(DataBroadcastingRouter.router)
app.include_router(NiconicoRouter.router)
app.include_router(TwitterRouter.router)
app.include_router(BlueskyRouter.router)
app.include_router(UsersRouter.router)
app.include_router(SettingsRouter.router)
app.include_router(MaintenanceRouter.router)
app.include_router(VersionRouter.router)

# CORS の設定
## 開発環境では全てのオリジンからのリクエストを許可
## 本番環境では app.konomi.tv 以外のオリジンからのリクエストを拒否
CORS_ORIGINS = ['*'] if CONFIG.general.debug is True else ['https://app.konomi.tv']
app.add_middleware(
    CORSMiddleware,
    allow_origins = CORS_ORIGINS,
    # すべての HTTP メソッドと HTTP ヘッダーを許可
    allow_methods = ['*'],
    allow_headers = ['*'],
    allow_credentials = True,
)

# reverse_proxy モードでは、実際の TCP 接続元を検証してから転送ヘッダーを反映する
## Uvicorn 標準の proxy headers は KonomiTV.py 側で無効化している
if CONFIG.server.https_mode == 'reverse_proxy':
    app.add_middleware(
        ReverseProxyMiddleware,
        trusted_proxy_cidrs = [str(cidr) for cidr in CONFIG.server.trusted_proxy_cidrs],
    )

# 拡張子と MIME タイプの対照表を上書きする
## StaticFiles の内部動作は mimetypes.guess_type() の挙動に応じて変化する
## 一部 Windows 環境では mimetypes.guess_type() が正しく機能しないため、明示的に指定しておく
for suffix, mime_type in [
    ('.css', 'text/css'),
    ('.html', 'text/html'),
    ('.ico', 'image/x-icon'),
    ('.js', 'application/javascript'),
    ('.json', 'application/json'),
    ('.map', 'application/json'),
    ]:
    guess = mimetypes.guess_type(f'foo{suffix}')[0]
    if guess != mime_type:
        mimetypes.add_type(mime_type, suffix)

# 静的ファイルの配信
app.mount('/assets', StaticFiles(directory=CLIENT_DIR / 'assets', html=True))

# ルート以下のルーティング (同期ファイル I/O を伴うため同期関数として実装している)
# ファイルが存在すればそのまま配信し、ファイルが存在しなければ index.html を返す
@app.get('/{file:path}', include_in_schema=False)
def Root(file: str):

    # ディレクトリトラバーサル対策のためのチェック
    ## ref: https://stackoverflow.com/a/45190125/17124142
    try:
        CLIENT_DIR.joinpath(Path(file)).resolve().relative_to(CLIENT_DIR.resolve())
    except ValueError:
        # URL に指定されたファイルパスが CLIENT_DIR の外側のフォルダを指している場合は、
        # ファイルが存在するかに関わらず一律で index.html を返す
        return FileResponse(CLIENT_DIR / 'index.html', media_type='text/html')

    # ファイルが存在する場合のみそのまま配信
    filepath = CLIENT_DIR / file
    if filepath.is_file():
        # 拡張子から MIME タイプを判定
        if filepath.suffix in ['.css', '.html', '.ico', '.js', '.json', '.map']:
            mime = mimetypes.guess_type(f'foo{filepath.suffix}')[0] or 'text/plain'
        else:
            mime = 'text/plain'
        return FileResponse(filepath, media_type=mime)

    # デフォルトドキュメント (index.html)
    # URL の末尾にスラッシュがついている場合のみ
    elif (filepath / 'index.html').is_file() and (file == '' or file[-1] == '/'):
        return FileResponse(filepath / 'index.html', media_type='text/html')

    # 存在しない静的ファイルが指定された場合
    else:
        if file.startswith('api/'):
            # パスに api/ が前方一致で含まれているなら、404 Not Found を返す
            return JSONResponse({'detail': 'Not Found'}, status_code = status.HTTP_404_NOT_FOUND)
        else:
            # パスに api/ が前方一致で含まれていなければ、index.html を返す
            return FileResponse(CLIENT_DIR / 'index.html', media_type='text/html')

def GetCORSHeadersForExceptionHandler(request: Request) -> dict[str, str]:
    """
    例外ハンドラ用の CORS ヘッダーを生成する。
    Starlette のミドルウェアスタックは ServerErrorMiddleware → CORSMiddleware → ExceptionMiddleware の順で構築される。
    @app.exception_handler() で登録したハンドラは ExceptionMiddleware で処理されるため、
    通常は CORSMiddleware の send ラッパーを通り CORS ヘッダーが付与される。
    ただし、ExceptionMiddleware で処理しきれない例外が ServerErrorMiddleware まで到達した場合、
    CORSMiddleware がバイパスされ CORS ヘッダーが欠落する。
    この関数はその防御策として、例外ハンドラ内で明示的に CORS ヘッダーを付与する。
    ref: https://github.com/fastapi/fastapi/discussions/8027
    ref: https://github.com/encode/starlette/discussions/2876

    Args:
        request: FastAPI の Request オブジェクト

    Returns:
        CORS ヘッダーを含む辞書。Origin が許可されていない場合は空の辞書を返す。
    """

    # リクエストの Origin ヘッダーを取得
    origin = request.headers.get('Origin')
    # CORS ヘッダーを設定
    cors_header = ''
    if origin is not None:
        # 開発環境では全てのオリジンからのリクエストを許可
        ## allow_credentials=True と Access-Control-Allow-Origin: * の組み合わせは
        ## ブラウザにブロックされるため、リクエスト元の Origin をそのままエコーバックする
        if CONFIG.general.debug is True:
            cors_header = origin
        # 本番環境では、Origin が許可されたオリジンに含まれている場合のみその Origin を返す
        elif origin in CORS_ORIGINS:
            cors_header = origin

    return {'Access-Control-Allow-Origin': cors_header} if cors_header else {}

# Internal Server Error のハンドリング
@app.exception_handler(Exception)
async def ExceptionHandler(request: Request, exc: Exception):
    return JSONResponse(
        {'detail': f'Oops! {type(exc).__name__} did something. There goes a rainbow...'},
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
        headers = GetCORSHeadersForExceptionHandler(request),
    )

# Tortoise ORM の初期化
## Tortoise ORM が利用するロガーを Uvicorn のロガーに差し替える
## ref: https://github.com/tortoise/tortoise-orm/issues/529
tortoise.log.logger = logging.logger
tortoise.log.db_client_logger = logging.logger
## Tortoise ORM を FastAPI に登録する
## ref: https://tortoise-orm.readthedocs.io/en/latest/contrib/fastapi.html
tortoise.contrib.fastapi.register_tortoise(
    app = app,
    config = DATABASE_CONFIG,
    generate_schemas = True,
    # Tortoise ORM の例外ハンドラ (DoesNotExist → 404, IntegrityError → 422) は登録しない
    ## これらのハンドラは CORS ヘッダーを付与しないため、ブラウザがエラーレスポンスをブロックする可能性がある
    ## Tortoise ORM の例外がルーターから漏れ出すこと自体がアプリのバグなので、
    ## @app.exception_handler(Exception) で 500 として処理すれば十分
    add_exception_handlers = False,
)

# サーバーの起動時に実行する
recorded_scan_task: RecordedScanTask | None = None
@app.on_event('startup')
async def Startup():
    global recorded_scan_task

    # 期限切れの更新トークンを起動時に削除し、認証テーブルの無制限増加を防ぐ
    await RefreshToken.cleanupExpired()

    # サーバー停止中を含めて期限切れになった OAuth state の PKCE verifier を起動直後に消去
    await NiconicoOAuthState.cleanupExpired()

    # 前回プロセスに残った構造化履歴とCM解析状態を中断へ確定する。
    await AnalysisTaskTracker.initialize()
    await CMAnalysisOrchestrator.markInterruptedAtStartup()

    # チャンネル情報を更新
    await Channel.update()

    # ニコニコ実況が有効ならステータスを更新し、無効なら以前のキャッシュと DB 値を消去する
    if CONFIG.general.jikkyo_enabled is True:
        await Channel.updateJikkyoStatus()
    else:
        await Channel.clearJikkyoStatus()

    # 番組情報を更新
    await Program.update()

    # migration前から存在し、現在Series所属済みの録画だけを、外部通信なしで構造化Episodeへ移行する。
    # 失敗してもPending行を残せるため、次回起動で同じ処理を安全に再試行できる。
    try:
        resolved_episode_count, review_episode_count = await RecordedEpisodeResolver.backfillLegacyEpisodes()
        if resolved_episode_count > 0 or review_episode_count > 0:
            logging.info(
                f'Legacy episode backfill completed. '
                f'[resolved: {resolved_episode_count}, needs_review: {review_episode_count}]',
            )
    except Exception as ex:
        logging.error('[RecordedEpisodeResolver] Failed to backfill legacy episodes:', exc_info=ex)

    # 録画スキャンとは分離したシリーズ判定ワーカーを開始する。
    await RecordedSeriesResolver.start()

    # Series確定後の話数解析・Web検索も別ワーカーで開始し、録画スキャンを待たせない。
    await RecordedEpisodeAutomation.start()

    # 全てのチャンネル&品質のライブストリームを初期化する
    for channel in await Channel.filter(is_watchable=True).order_by('channel_number'):
        for quality in QUALITY:
            LiveStream(channel.display_channel_id, quality)

    # 録画スキャナーを起動する前に、前プロセスが残した非稼働CM解析workspaceだけを回収する。
    await CMAnalysisWorkspace.cleanupStale()

    # DBから到達不能になった字幕cacheを、録画スキャナー開始前にも安全に回収する。
    await RecordedSubtitleStream.cleanupOrphanedCaches()

    # 録画フォルダ監視・メタデータ更新/同期タスクを開始
    ## 録画ファイルの量次第では録画ファイルの更新確認に時間がかかるため、非同期で実行する
    # ref: https://docs.astral.sh/ruff/rules/asyncio-dangling-task/
    recorded_scan_task = RecordedScanTask()
    await recorded_scan_task.start()

    # 録画再生用インデックスの中断状態を復旧し、低優先度バックフィルを開始する。
    await RecordedPlaybackIndexer.start()

    # 異常終了した前プロセスが残したfMP4予約キャッシュだけを削除する。
    await RecordedFMP4CacheManager.cleanupStale()

# サーバー設定で指定された時間 (デフォルト: 15分) ごとに1回、チャンネル情報と番組情報を更新する
# チャンネル情報は頻繁に変わるわけではないけど、手動で再起動しなくても自動で変更が適用されてほしい
# 番組情報の更新処理はかなり重くストリーム配信などの他の処理に影響してしまうため、マルチプロセスで実行する
@app.on_event('startup')
@repeat_every(
    seconds = CONFIG.general.program_update_interval * 60,
    wait_first = CONFIG.general.program_update_interval * 60,
    logger = logging.logger,
)
async def UpdateChannelAndProgram():
    await Channel.update()
    # 無効時には定期処理から実況の外部 API を呼び出さない
    if CONFIG.general.jikkyo_enabled is True:
        await Channel.updateJikkyoStatus()
    await Program.update(multiprocess=True)

# 30秒に1回、ニコニコ実況関連のステータスを更新する
@app.on_event('startup')
@repeat_every(seconds=0.5 * 60, wait_first=0.5 * 60, logger=logging.logger)
async def UpdateChannelJikkyoStatus():
    # 無効時には30秒周期の実況更新処理自体を実行しない
    if CONFIG.general.jikkyo_enabled is True:
        await Channel.updateJikkyoStatus()

# 1分に1回、放置された期限切れ OAuth state の PKCE verifier を消去する
# OAuth が再度利用されない環境でも、秘密文字列の保持期間を有効期限後1分以内に制限する
@app.on_event('startup')
@repeat_every(seconds=1 * 60, wait_first=1 * 60, logger=logging.logger)
async def CleanupExpiredNiconicoOAuthStates():
    await NiconicoOAuthState.cleanupExpired()

# サーバーの終了処理は FastAPI と atexit のどちらから呼ばれても同じ Task を共有する
_shutdown_completed = False
_shutdown_task: asyncio.Task[None] | None = None


async def _RunShutdownCleanup() -> None:
    """
    アプリ終了時にライブストリームとバックグラウンド資源をすべて停止する。

    Args:
        None

    Returns:
        None
    """

    cleanup_failures: list[BaseException] = []

    async def RunCleanupStep(
        label: str,
        operation: Awaitable[object],
    ) -> tuple[bool, object | None]:
        """一工程の失敗後も残りの終了処理を続け、成否と結果を返す。"""

        try:
            return True, await operation
        except BaseException as ex:
            cleanup_failures.append(ex)
            logging.error(f'{label} Shutdown cleanup failed.', exc_info=ex)
            return False, None

    # 単一パイプライン版の全ライブストリームを Offline にして終了させる。
    for live_stream in LiveStream.getAllLiveStreams():
        live_stream.setStatus('Offline', 'ライブストリームは Offline です。', True)

    # 全てのチューナーインスタンスを終了する (ライブ放送波を EDCB から受信している場合のみ)
    if CONFIG.general.live_stream_backend == 'EDCB':
        await RunCleanupStep('[EDCBTuner]', EDCBTuner.closeAll())

    # 録画フォルダ監視タスクを停止
    global recorded_scan_task
    if recorded_scan_task is not None:
        scan_stop_succeeded, _scan_stop_result = await RunCleanupStep(
            '[RecordedScanTask]',
            recorded_scan_task.stop(),
        )
        if scan_stop_succeeded:
            recorded_scan_task = None

    # DB接続が閉じられる前にproducerのシリーズ判定を先に止め、その後に話数判定を停止する。
    # 逆順では、停止済みの話数ワーカーへSeries側がenqueueして再起動する競合が起こり得る。
    await RunCleanupStep('[RecordedSeriesResolver]', RecordedSeriesResolver.stop())
    await RunCleanupStep('[RecordedEpisodeAutomation]', RecordedEpisodeAutomation.stop())

    # DB接続が閉じられる前に、HTTP接続から分離した手動CM再判定を中断・回収する。
    await RunCleanupStep('[CMAnalysisTaskManager]', CMAnalysisTaskManager.stop())

    # DB接続が閉じられる前に録画再生用インデックスワーカーを停止する。
    await RunCleanupStep('[RecordedPlaybackIndexer]', RecordedPlaybackIndexer.stop())

    if cleanup_failures:
        raise RuntimeError('shutdown_cleanup_failed') from BaseExceptionGroup(
            'application shutdown cleanup failures',
            cleanup_failures,
        )


async def _RunShutdownCleanupOnce() -> None:
    """
    終了処理本体を実行し、例外なく完了した場合だけ完了状態を確定する。

    Returns:
        None
    """

    global _shutdown_completed
    await _RunShutdownCleanup()
    _shutdown_completed = True


@app.on_event('shutdown')
async def Shutdown() -> None:
    """
    同時・再呼び出し間で終了処理 Task を共有し、呼び出し側のキャンセルから保護して待機する。

    Returns:
        None
    """

    global _shutdown_task
    if _shutdown_completed is True:
        return

    running_loop = asyncio.get_running_loop()
    shutdown_task = _shutdown_task
    if shutdown_task is not None:
        if shutdown_task.done() is True:
            # 失敗またはキャンセルで完了状態が確定しなかった Task は再試行対象とする。
            _shutdown_task = None
            shutdown_task = None
        elif shutdown_task.get_loop() is not running_loop:
            # atexit は FastAPI の event loop 終了後に別 loop で呼ばれる。
            # 閉じた loop に残った未完 Task は進行不能なので、新しい loop で cleanup を再試行する。
            if shutdown_task.get_loop().is_closed() is False:
                raise RuntimeError('Shutdown cleanup is already running on another event loop.')
            _shutdown_task = None
            shutdown_task = None

    if shutdown_task is None:
        shutdown_task = running_loop.create_task(
            _RunShutdownCleanupOnce(),
            name = 'app-shutdown-cleanup',
        )
        _shutdown_task = shutdown_task

    try:
        # shutdown event の呼び出し側だけがキャンセルされても、共有 cleanup Task は継続させる。
        await asyncio.shield(shutdown_task)
    except BaseException:
        # cleanup Task 自体が失敗またはキャンセルされた場合だけ参照を外し、次回呼び出しを再試行可能にする。
        if shutdown_task.done() is True and _shutdown_task is shutdown_task:
            _shutdown_task = None
        raise


# shutdown イベントが発火しない場合も想定し、アプリケーションの終了時に Shutdown() が確実に呼ばれるように
def _ShutdownAtExit() -> None:
    """
    FastAPI 側で完了していない終了処理を、interpreter 終了時の新しい event loop で再試行する。

    Returns:
        None
    """

    if _shutdown_completed is True:
        return
    try:
        asyncio.run(Shutdown())
    except BaseException as ex:
        logging.error('Shutdown cleanup failed during interpreter exit.', exc_info=ex)


atexit.register(_ShutdownAtExit)

# 互換 API は通常 API と同じプロセス・DB・ストリーム状態を共有する一方、別の FastAPI ルーター集合を使う。
# lifespan は PortDispatchApplication が通常アプリだけへ転送するため、起動・終了処理が二重に走ることはない。
application: ASGIApp = app
if CONFIG.compatibility_api.enabled:
    compatibility_https_settings = ResolveCompatibilityHTTPSSettings(CONFIG)
    trusted_proxy_cidrs = None
    if compatibility_https_settings.https_mode == 'reverse_proxy':
        trusted_proxy_cidrs = [str(cidr) for cidr in compatibility_https_settings.trusted_proxy_cidrs]
    compatibility_app = CreateCompatibilityAPI(
        profile = CONFIG.compatibility_api.profile,
        trusted_proxy_cidrs = trusted_proxy_cidrs,
    )
    compatibility_startup_settings = BuildServerStartupSettings(
        compatibility_https_settings,
        port = CONFIG.compatibility_api.port,
    )
    application = PortDispatchApplication(
        main_app = app,
        compatibility_app = compatibility_app,
        compatibility_port = compatibility_startup_settings.port,
    )
