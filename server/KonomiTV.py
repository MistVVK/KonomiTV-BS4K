
import asyncio
import atexit
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import typer
import uvicorn
from aerich import Command
from tortoise import Tortoise
from uvicorn.supervisors.watchfilesreload import WatchFilesReload

from app.config import LoadConfig
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
)
from app.utils.LogRotation import SplitServerLogByDate


# passlib が送出する bcrypt のバージョン差異による警告を無視
# ref: https://github.com/pyca/bcrypt/issues/684
logging.getLogger('passlib').setLevel(logging.ERROR)

cli = typer.Typer()

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

    # akebi モードの場合だけ Akebi バイナリを確認し、前回の Akebi ログを削除する
    if CONFIG.server.https_mode == 'akebi':
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

    # akebi モードの場合だけ Akebi Keyless Server を起動する
    reverse_proxy_process: subprocess.Popen[bytes] | None = None
    if startup_settings.use_akebi:
        with open(AKEBI_LOG_PATH, mode='w', encoding='utf-8') as file:
            reverse_proxy_process = subprocess.Popen(
                [
                    LIBRARY_PATH['Akebi'],
                    '--listen-address', f'0.0.0.0:{CONFIG.server.port}',
                    '--proxy-pass-url', f'http://{startup_settings.host}:{startup_settings.port}/',
                    '--keyless-server-url', 'https://akebi.konomi.tv/',
                ],
                stdout = file,
                stderr = file,
            )

        # このプロセスが終了されたときに、Akebi も一緒に終了する
        atexit.register(reverse_proxy_process.terminate)

        # upstream インストーラーと同じ規則で、Akebi の証明書を利用してアクセスできる URL を表示する
        logging.info('KonomiTV にアクセスできる URL:')
        for access_url, interface_name in GetAkebiAccessURLs(CONFIG.server.port):
            logging.info(f'  {access_url} ({interface_name})')

    # Uvicorn の設定
    server_config = uvicorn.Config(
        # 起動するアプリケーション
        app = 'app.app:app',
        # リッスンするアドレス
        host = startup_settings.host,
        port = startup_settings.port,
        ssl_certfile = startup_settings.ssl_certfile,
        ssl_keyfile = startup_settings.ssl_keyfile,
        # TCP 接続元の CIDR 検証前に転送ヘッダーを反映させない
        proxy_headers = startup_settings.proxy_headers,
        # 自動リロードモードモードで起動するか
        reload = reload,
        # リロードするフォルダ
        reload_dirs = str(BASE_DIR / 'app') if reload else None,
        # ロギングの設定
        log_config = LOGGING_CONFIG,
        # インターフェイスとして ASGI3 を選択
        interface = 'asgi3',
        # HTTP プロトコルの実装として httptools を選択
        http = 'httptools',
        # イベントループのセットアップは自前で行うため、ここでは none を指定
        loop = 'none',
        # ストリーミング配信中にサーバーシャットダウンを要求された際、強制的に接続を切断するまでの秒数
        timeout_graceful_shutdown = 1,
    )

    # Uvicorn のサーバーインスタンスを初期化
    server = uvicorn.Server(server_config)

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
            sock = server_config.bind_socket()
            WatchFilesReload(server_config, target=server.run, sockets=[sock]).run()
        else:
            # 通常時
            server.run()
    except KeyboardInterrupt:
        # Uvicorn のサーバーインスタンスから KeyboardInterrupt が送出された場合は一旦無視する
        # 少し前の Uvicorn は KeyboardInterrupt を内部で握り潰していたが、最近のバージョンから送出するようになった
        pass

    # akebi モードの場合だけ Akebi を終了する
    if reverse_proxy_process is not None:
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
