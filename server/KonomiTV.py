
import asyncio
import atexit
import logging
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import typer
import uvicorn
from aerich import Command
from tortoise import Tortoise
from uvicorn.supervisors.watchfilesreload import WatchFilesReload

from app.config import LoadConfig, ResolveCompatibilityHTTPSSettings
from app.constants import (
    AKEBI_LOG_PATH,
    BASE_DIR,
    DATABASE_CONFIG,
    KONOMITV_ACCESS_LOG_PATH,
    LIBRARY_PATH,
    LOGGING_CONFIG,
    RESTART_REQUIRED_LOCK_PATH,
    VERSION,
)
from app.utils.HTTPS import (
    BuildServerStartupSettings,
    GetAkebiAccessURLs,
    GetRequiredThirdpartyLibraries,
    ServerStartupSettings,
)
from app.utils.LogRotation import SplitServerLogByDate


# passlib が送出する bcrypt のバージョン差異による警告を無視
# ref: https://github.com/pyca/bcrypt/issues/684
logging.getLogger('passlib').setLevel(logging.ERROR)

cli = typer.Typer()


def BindUvicornSocket(server_config: uvicorn.Config, startup_settings: ServerStartupSettings) -> socket.socket:
    """指定された Uvicorn 設定でリスナ用のソケットを事前に確保する。"""

    original_host = server_config.host
    original_port = server_config.port
    try:
        server_config.host = startup_settings.host
        server_config.port = startup_settings.port
        return server_config.bind_socket()
    finally:
        server_config.host = original_host
        server_config.port = original_port


class MultiListenerUvicornServer(uvicorn.Server):
    """リスナごとに異なる HTTP/TLS 設定を適用し、lifespan と状態は共有する Uvicorn Server。"""

    def __init__(self, listener_configs: list[uvicorn.Config]) -> None:
        if not listener_configs:
            raise ValueError('At least one Uvicorn listener config is required.')
        super().__init__(listener_configs[0])
        self.listener_configs = listener_configs

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """通常 API の lifespan を一度だけ起動し、各ソケットへ固有の TLS 設定を割り当てる。"""

        if sockets is None or len(sockets) != len(self.listener_configs):
            raise ValueError('One pre-bound socket is required for each Uvicorn listener config.')

        # 証明書の読み込みなどで失敗する設定は、通常 API の lifespan 起動前に検出する。
        for listener_config in self.listener_configs[1:]:
            if listener_config.loaded is False:
                listener_config.load()

        # 通常 API は Uvicorn 標準の起動経路を使い、lifespan と共有 ServerState を初期化する。
        await super().startup(sockets=[sockets[0]])
        if self.should_exit:
            return

        loop = asyncio.get_running_loop()
        try:
            for listener_config, listener_socket in zip(self.listener_configs[1:], sockets[1:]):
                def CreateProtocol(
                    _loop: asyncio.AbstractEventLoop | None = None,
                    config: uvicorn.Config = listener_config,
                ) -> asyncio.Protocol:
                    protocol_class = cast(Callable[..., asyncio.Protocol], config.http_protocol_class)
                    return protocol_class(
                        config = config,
                        server_state = self.server_state,
                        app_state = self.lifespan.state,
                        _loop = _loop,
                    )

                listener_server = await loop.create_server(
                    CreateProtocol,
                    sock = listener_socket,
                    ssl = listener_config.ssl,
                    backlog = listener_config.backlog,
                )
                self.servers.append(listener_server)
        except BaseException:
            # 途中のリスナ起動に失敗した場合、通常 API だけが残らないように一括停止する。
            await super().shutdown(sockets=sockets)
            self.started = False
            raise


def CreateUvicornConfig(
    startup_settings: ServerStartupSettings,
    *,
    reload: bool,
) -> uvicorn.Config:
    """1つの公開 API リスナに対応する Uvicorn 設定を作成する。"""

    return uvicorn.Config(
        app = 'app.app:application',
        host = startup_settings.host,
        port = startup_settings.port,
        ssl_certfile = startup_settings.ssl_certfile,
        ssl_keyfile = startup_settings.ssl_keyfile,
        # TCP 接続元の CIDR 検証前に転送ヘッダーを反映させない
        proxy_headers = startup_settings.proxy_headers,
        reload = reload,
        reload_dirs = str(BASE_DIR / 'app') if reload else None,
        log_config = LOGGING_CONFIG,
        interface = 'asgi3',
        http = 'httptools',
        # イベントループのセットアップは KonomiTV.py 側で行う
        loop = 'none',
        timeout_graceful_shutdown = 1,
    )

def version(value: bool):
    if value is True:
        typer.echo(f'KonomiTV version {VERSION}')
        raise typer.Exit()

@cli.command(help='KonomiTV: Kept Organized, Notably Optimized, Modern Interface TV media server')
def main(
    reload: bool = typer.Option(False, '--reload', help='Start Uvicorn in auto-reload mode. (Linux only)'),
    version: bool = typer.Option(None, '--version', callback=version, is_eager=True, help='Show version information.'),
):

    # タイムゾーンを常に Asia/Tokyo に設定する
    ## タイムゾーンが UTC の環境ではログの日時が日本時間より9時間遅れてしまうため
    ## デフォルトを Asia/Tokyo に変更することで、万が一のタイムゾーン関連のバグを防ぐ防波堤としての意味合いもある
    os.environ['TZ'] = 'Asia/Tokyo'
    time.tzset()

    # 前回のアクセスログを削除する
    ## サーバーログは起動時に日付別分割されるため、ここでは削除しない
    try:
        if KONOMITV_ACCESS_LOG_PATH.exists():
            KONOMITV_ACCESS_LOG_PATH.unlink()
    except PermissionError:
        pass

    # サーバーログに過去日付のエントリが含まれている場合、日付別アーカイブに分割する
    ## DailyRotatingFileHandler がファイルを開く前に分割を完了させるために、ロガーの初期化前に実行する必要がある
    SplitServerLogByDate()

    # もし何らかの理由でロックファイルが残っていた場合は削除する
    if RESTART_REQUIRED_LOCK_PATH.exists():
        RESTART_REQUIRED_LOCK_PATH.unlink()

    # ここでロガーとユーティリティをインポートする
    ## 前回のログを削除する前でないと正しく動作しない
    ## ロギング設定は logging.py が読み込まれた瞬間に行われるが、その際に前回のログファイルが残っているとエラーになる
    ## 前回のログをすべて削除する処理を logging.py 自体に記述してしまうとマルチプロセス実行時や自動リロードモード時に意図せずファイルが削除されてしまう
    ## constants.py は内部モジュールへの依存がなく、config.py も constants.py 以外への依存はないので、この2つのみトップレベルでインポートしている
    from app import logging

    # バージョン情報をログに出力
    logging.info(f'KonomiTV version {VERSION}')

    # Aerich でデータベースをアップグレードする
    ## 特にデータベースのアップグレードが必要ない場合は何も起こらない
    async def UpgradeDatabase():
        command = Command(tortoise_config=DATABASE_CONFIG, app='models', location='./app/migrations/')
        await command.init()
        migrated = await command.upgrade(run_in_transaction=True)
        await Tortoise.close_connections()
        if not migrated:
            logging.info('No database migration is required.')
        else:
            for version_file in migrated:
                logging.info(f'Successfully migrated to {version_file}.')
    asyncio.run(UpgradeDatabase())

    # ***** サポートされているアーキテクチャかのバリデーション *****

    # CPU のアーキテクチャから実行可否を判定
    # Docker image は Linux amd64 専用なので、実行環境も同じ条件に限定する
    if sys.platform != 'linux' or os.uname().machine != 'x86_64':
        logging.error('KonomiTV は Linux amd64 Docker 環境でのみ実行できます。')
        sys.exit(1)

    # ***** サードパーティーライブラリが配置されているかのバリデーション *****

    # HTTPS モードに依存しないサードパーティーライブラリの配置をチェック
    ## Akebi は設定ロード後に akebi モードの場合だけ確認する
    for library_name in GetRequiredThirdpartyLibraries('certificate'):
        library_path = LIBRARY_PATH[library_name]
        if Path(library_path).is_file() is False:
            logging.error(f'{library_name} がサードパーティーライブラリとして配置されていないため、KonomiTV を起動できません。')
            logging.error(f'{library_name} が {library_path} に配置されているかを確認してください。')
            sys.exit(1)

    # ***** サーバー設定データのロード *****

    # サーバー設定データのロードとバリデーションを行う
    ## ここでロードしたサーバー設定データが Config() で参照される
    ## config.yaml が配置されていなかったりバリデーションエラーが発生した際は、
    ## LoadConfig() 内でエラーログを出力した後、sys.exit(1) でサーバーが終了される
    CONFIG = LoadConfig()

    compatibility_https_settings = (
        ResolveCompatibilityHTTPSSettings(CONFIG)
        if CONFIG.compatibility_api.enabled
        else None
    )
    akebi_is_required = (
        CONFIG.server.https_mode == 'akebi' or
        (compatibility_https_settings is not None and compatibility_https_settings.https_mode == 'akebi')
    )

    # 通常 API または互換 API が akebi モードの場合だけ、Akebi バイナリを確認して前回ログを削除する
    if akebi_is_required:
        akebi_path = LIBRARY_PATH['Akebi']
        if Path(akebi_path).is_file() is False:
            logging.error('Akebi がサードパーティーライブラリとして配置されていないため、KonomiTV を起動できません。')
            logging.error(f'Akebi が {akebi_path} に配置されているかを確認してください。')
            sys.exit(1)
        try:
            if AKEBI_LOG_PATH.exists():
                AKEBI_LOG_PATH.unlink()
        except PermissionError:
            pass

    # ***** KonomiTV サーバーを起動 *****

    startup_settings = BuildServerStartupSettings(CONFIG.server)
    compatibility_startup_settings: ServerStartupSettings | None = None
    if compatibility_https_settings is not None:
        compatibility_startup_settings = BuildServerStartupSettings(
            compatibility_https_settings,
            port = CONFIG.compatibility_api.port,
        )

    # akebi モードのリスナごとに Akebi Keyless Server を起動する
    reverse_proxy_processes: list[subprocess.Popen[bytes]] = []
    akebi_listeners: list[tuple[int, ServerStartupSettings]] = []
    if startup_settings.use_akebi:
        akebi_listeners.append((CONFIG.server.port, startup_settings))
    if compatibility_startup_settings is not None and compatibility_startup_settings.use_akebi:
        akebi_listeners.append((CONFIG.compatibility_api.port, compatibility_startup_settings))
    if akebi_listeners:
        # 従来どおりサーバー起動ごとにログを初期化し、各 Akebi は同じファイルへ追記する。
        with open(AKEBI_LOG_PATH, mode='w', encoding='utf-8'):
            pass
        for public_port, internal_settings in akebi_listeners:
            # 複数の Akebi が同時に書き込んでもログを上書きしないよう、追記モードで共通ログを開く。
            with open(AKEBI_LOG_PATH, mode='a', encoding='utf-8') as file:
                reverse_proxy_process = subprocess.Popen(
                    [
                        LIBRARY_PATH['Akebi'],
                        '--listen-address', f'0.0.0.0:{public_port}',
                        '--proxy-pass-url', f'http://{internal_settings.host}:{internal_settings.port}/',
                        '--keyless-server-url', 'https://akebi.konomi.tv/',
                    ],
                    stdout = file,
                    stderr = file,
                )
            reverse_proxy_processes.append(reverse_proxy_process)

            # このプロセスが終了されたときに、Akebi も一緒に終了する
            atexit.register(reverse_proxy_process.terminate)

        # upstream インストーラーと同じ規則で、Akebi の証明書を利用してアクセスできる URL を表示する
        if startup_settings.use_akebi:
            logging.info('KonomiTV にアクセスできる URL:')
            for access_url, interface_name in GetAkebiAccessURLs(CONFIG.server.port):
                logging.info(f'  {access_url} ({interface_name})')
        if compatibility_startup_settings is not None and compatibility_startup_settings.use_akebi:
            logging.info(f'{CONFIG.compatibility_api.profile} 互換 API にアクセスできる URL:')
            for access_url, interface_name in GetAkebiAccessURLs(CONFIG.compatibility_api.port):
                logging.info(f'  {access_url} ({interface_name})')

    # リスナごとに HTTP/TLS 設定を分離しつつ、単一 Uvicorn Server の lifespan と状態を共有する。
    # これにより通常 API と互換 API で、HTTP・Akebi・直接 TLS・証明書を独立して選択できる。
    server_configs = [CreateUvicornConfig(startup_settings, reload=reload)]
    server_sockets = [BindUvicornSocket(server_configs[0], startup_settings)]
    if compatibility_startup_settings is not None:
        compatibility_server_config = CreateUvicornConfig(compatibility_startup_settings, reload=False)
        server_configs.append(compatibility_server_config)
        server_sockets.append(BindUvicornSocket(compatibility_server_config, compatibility_startup_settings))

    # Uvicorn のサーバーインスタンスを初期化
    server = MultiListenerUvicornServer(server_configs)
    server_config = server_configs[0]

    # Linux では Uvloop をイベントループとして利用する
    import uvloop
    uvloop.install()

    # Uvicorn を起動
    ## 自動リロードモードと通常時で呼び方が異なる
    ## ここで終了までブロッキングされる（非同期 I/O のエントリーポイント）
    ## ref: https://github.com/encode/uvicorn/blob/0.18.2/uvicorn/main.py#L568-L575
    try:
        if server_config.should_reload:
            # 自動リロードモード
            WatchFilesReload(server_config, target=server.run, sockets=server_sockets).run()
        else:
            # 通常時
            server.run(sockets=server_sockets)
    except KeyboardInterrupt:
        # Uvicorn のサーバーインスタンスから KeyboardInterrupt が送出された場合は一旦無視する
        # 少し前の Uvicorn は KeyboardInterrupt を内部で握り潰していたが、最近のバージョンから送出するようになった
        pass

    # akebi モードの場合だけ Akebi を終了する
    for reverse_proxy_process in reverse_proxy_processes:
        reverse_proxy_process.terminate()

    # この時点ではタイミングの関係でまだロックファイルが作成されていないことがあるので、1秒待機する
    time.sleep(1)

    # もしこの時点で再起動が必要であることを示すロックファイルが存在する場合、KonomiTV サーバーを再起動する
    ## このロックファイルは ServerRestartAPI によって作成される
    if RESTART_REQUIRED_LOCK_PATH.exists():
        logging.warning('Server restart requested. Restarting...')

        # os.execv() で現在のプロセスを新規に起動したプロセスに置き換える
        ## os.execv() は戻らないので、事前にロックファイルを削除しておく
        RESTART_REQUIRED_LOCK_PATH.unlink()
        os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == '__main__':
    cli()
