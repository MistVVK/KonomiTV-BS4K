
import base64
import hashlib
import os
import pkgutil
import secrets
import stat
import sys
import time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet
from passlib.context import CryptContext
from pydantic import BaseModel, PositiveInt


# upstream KonomiTV のバージョン
VERSION = '0.14.1'
# KonomiTV-BS4K 固有のバージョン
BS4K_VERSION = '1.0.0'

# 日本標準時 (JST, UTC+9) の ZoneInfo
## KonomiTV は日本向けのアプリケーションのため、日時は JST で統一して扱う
JST = ZoneInfo('Asia/Tokyo')

# ベースディレクトリ
BASE_DIR = Path(__file__).resolve().parent.parent

# third-party ソフトウェアのライセンス文書
THIRD_PARTY_LICENSES_PATH = BASE_DIR.parent / 'THIRD_PARTY_LICENSES.md'

# クライアントの静的ファイルがあるディレクトリ
CLIENT_DIR = BASE_DIR.parent / 'client/dist'

# データディレクトリ
DATA_DIR = BASE_DIR / 'data'
## アカウントのアイコン画像があるディレクトリ
ACCOUNT_ICON_DIR = DATA_DIR / 'account-icons'
## サムネイル画像があるディレクトリ
THUMBNAILS_DIR = DATA_DIR / 'thumbnails'
## 変換済み録画字幕 (WebVTT) のキャッシュディレクトリ
RECORDED_SUBTITLES_DIR = DATA_DIR / 'recorded-subtitles'
## Twitter 関連のデバッグ用スクリーンショットの保存先ディレクトリ
TWITTER_DEBUG_SCREENSHOTS_DIR = DATA_DIR / 'twitter-debug-screenshots'
## デバッグ用スクリーンショットの保持期限 (日数)
## 7 日を超えたスクリーンショットを自動削除する
TWITTER_DEBUG_SCREENSHOTS_RETENTION_DAYS = 7
## サーバー終了時に再起動が必要なことを伝えるロックファイルのパス
RESTART_REQUIRED_LOCK_PATH = DATA_DIR / 'restart_required.lock'

# スタティックディレクトリ
STATIC_DIR = BASE_DIR / 'static'
## ロゴファイルがあるディレクトリ
LOGO_DIR = STATIC_DIR / 'logos'
## デフォルトのアイコン画像があるディレクトリ
ACCOUNT_ICON_DEFAULT_DIR = STATIC_DIR / 'account-icons'
## jikkyo_channels.json があるパス
JIKKYO_CHANNELS_PATH = STATIC_DIR / 'jikkyo_channels.json'

# ログディレクトリ
LOGS_DIR = BASE_DIR / 'logs'
## サーバーログのアーカイブ（日付別ログ）を格納するサブディレクトリ
## ログディレクトリ直下にアーカイブが大量に並ぶとノイズになるため、サブディレクトリに分離する
LOGS_ARCHIVES_DIR = LOGS_DIR / 'archives'
## サーバーログのアーカイブの保持期限 (日数)
## 30 日を超えたアーカイブログを自動削除する
SERVER_LOG_ARCHIVE_RETENTION_DAYS: int | None = 30
## KonomiTV-BS4K のサーバーログのパス
KONOMITV_SERVER_LOG_PATH = LOGS_DIR / 'KonomiTV-BS4K-Server.log'
## KonomiTV-BS4K のアクセスログのパス
KONOMITV_ACCESS_LOG_PATH = LOGS_DIR / 'KonomiTV-BS4K-Access.log'
## Akebi (HTTPS リバースプロキシ) のログファイルのパス
AKEBI_LOG_PATH = LOGS_DIR / 'Akebi-HTTPS-Server.log'
## 製品用 opencode serve のログファイルのパス
OPENCODE_SERVE_LOG_PATH = LOGS_DIR / 'opencode-serve.log'

# サードパーティーライブラリのあるディレクトリ
LIBRARY_DIR = BASE_DIR / 'thirdparty'

# サードパーティーライブラリのあるパス
LIBRARY_PATH = {
    'Akebi': str(LIBRARY_DIR / 'Akebi/akebi-https-server.elf'),
    'FFmpeg8': str(LIBRARY_DIR / 'FFmpeg8/ffmpeg8.elf'),
    'FFmpeg8AMD': str(LIBRARY_DIR / 'FFmpeg8/ffmpeg8-amd.sh'),
    'FFprobe8': str(LIBRARY_DIR / 'FFmpeg8/ffprobe8.elf'),
    'KonomiTVBS4KTSCodecBridge': str(
        LIBRARY_DIR / 'KonomiTVBS4KTSCodecBridge/ts-codec-bridge.elf'
    ),
    'tsreadex': str(LIBRARY_DIR / 'tsreadex/tsreadex.elf'),
    'psisiarc': str(LIBRARY_DIR / 'psisiarc/psisiarc.elf'),
    # 製品用 opencode serve バイナリ（Docker 同梱 SEA）。ホスト開発時は PATH 上の同名でも可。
    'OpenCode': '/usr/local/bin/opencode',
}

# ----- 製品用 opencode serve（録画シリーズ AI）-----
# 監査用 OpenCode（port 4096 想定）と port / home / workspace / auth を完全分離する。
OPENCODE_SERVE_HOST = '127.0.0.1'
OPENCODE_SERVE_PORT = 4097
OPENCODE_SERVE_BASE_URL = f'http://{OPENCODE_SERVE_HOST}:{OPENCODE_SERVE_PORT}'
## config / data / PID を置く製品専用 home ルート
OPENCODE_HOME_ROOT = DATA_DIR / 'opencode-home'
## XDG_CONFIG_HOME（opencode.json / auth.json 等がぶら下がる）
OPENCODE_XDG_CONFIG_HOME = OPENCODE_HOME_ROOT / 'config'
## XDG_DATA_HOME
OPENCODE_XDG_DATA_HOME = OPENCODE_HOME_ROOT / 'data'
## serve の cwd。ソースツリーを置かない空の最小化 workspace
OPENCODE_WORKSPACE_DIR = DATA_DIR / 'opencode-workspace'
## orphan 回収用 PID ファイル
OPENCODE_SERVE_PID_PATH = OPENCODE_HOME_ROOT / 'opencode-serve.pid'
## イメージ同梱の製品用 config 雛形（初回 seed 元）
OPENCODE_BUNDLED_CONFIG_PATH = Path('/usr/local/share/konomitv-bs4k-opencode/opencode.json')
## リポジトリ内の雛形（開発・テスト用フォールバック）
OPENCODE_REPO_CONFIG_PATH = BASE_DIR.parent / 'docker' / 'opencode' / 'opencode.json'
## health リトライ（起動直後の bind 待ち）
OPENCODE_HEALTH_RETRY_ATTEMPTS = 30
OPENCODE_HEALTH_RETRY_INTERVAL_SEC = 0.2
OPENCODE_HEALTH_TIMEOUT_SEC = 2.0
## 固定 version（Dockerfile / package.json と一致させる）
OPENCODE_PINNED_VERSION = '1.18.13'
## 生成 agent / EpisodeLookup agent 名（opencode.json と一致）
OPENCODE_AGENT_GENERATE = 'recorded-series-generate'
OPENCODE_AGENT_EPISODE = 'recorded-series-episode'

# データベース (Tortoise ORM) の設定
__model_list = [name for _, name, _ in pkgutil.iter_modules(path=['app/models'])]
DATABASE_CONFIG = {
    'timezone': 'Asia/Tokyo',
    'connections': {
        'default': f'sqlite://{DATA_DIR / "database.sqlite"!s}',
    },
    'apps': {
        'models': {
            'models': [f'app.models.{name}' for name in __model_list] + ['aerich.models'],
            'default_connection': 'default',
        }
    }
}

# Uvicorn のロギング設定
## この dictConfig を Uvicorn に渡す (KonomiTV 本体のロギング設定は app.logging に別で存在する)
## Uvicorn のもとの dictConfig を参考にして作成した
## ref: https://github.com/encode/uvicorn/blob/0.18.2/uvicorn/config.py#L95-L126
LOGGING_CONFIG: dict[str, Any] = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        # サーバーログ用のログフォーマッター
        'default': {
            '()': 'uvicorn.logging.DefaultFormatter',
            'datefmt': '%Y/%m/%d %H:%M:%S',
            'format': '[%(asctime)s.%(msecs)03d] %(levelprefix)s %(message)s',
        },
        'default_file': {
            '()': 'uvicorn.logging.DefaultFormatter',
            'datefmt': '%Y/%m/%d %H:%M:%S',
            'format': '[%(asctime)s.%(msecs)03d] %(levelprefix)s %(message)s',
            'use_colors': False,  # ANSI エスケープシーケンスを出力しない
        },
        # サーバーログ (デバッグ) 用のログフォーマッター
        'debug': {
            '()': 'uvicorn.logging.DefaultFormatter',
            'datefmt': '%Y/%m/%d %H:%M:%S',
            'format': '[%(asctime)s.%(msecs)03d] %(levelprefix)s %(pathname)s:%(lineno)s:\n'
            '                                %(message)s',
        },
        'debug_file': {
            '()': 'uvicorn.logging.DefaultFormatter',
            'datefmt': '%Y/%m/%d %H:%M:%S',
            'format': '[%(asctime)s.%(msecs)03d] %(levelprefix)s %(pathname)s:%(lineno)s:\n'
            '                                %(message)s',
            'use_colors': False,  # ANSI エスケープシーケンスを出力しない
        },
        # アクセスログ用のログフォーマッター
        'access': {
            '()': 'uvicorn.logging.AccessFormatter',
            'datefmt': '%Y/%m/%d %H:%M:%S',
            'format': '[%(asctime)s.%(msecs)03d] %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
        },
        'access_file': {
            '()': 'uvicorn.logging.AccessFormatter',
            'datefmt': '%Y/%m/%d %H:%M:%S',
            'format': '[%(asctime)s.%(msecs)03d] %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            'use_colors': False,  # ANSI エスケープシーケンスを出力しない
        },
    },
    'handlers': {
        # サーバーログは標準エラー出力と server/logs/KonomiTV-BS4K-Server.log の両方に出力する
        'default': {
            'formatter': 'default',
            'class': 'logging.StreamHandler',
            'stream': 'ext://sys.stderr',
        },
        'default_file': {
            'formatter': 'default_file',
            'class': 'app.utils.LogRotation.DailyRotatingFileHandler',
            'filename': KONOMITV_SERVER_LOG_PATH,
            'encoding': 'utf-8',
            'retention_days': SERVER_LOG_ARCHIVE_RETENTION_DAYS,
        },
        # サーバーログ (デバッグ) は標準エラー出力と server/logs/KonomiTV-BS4K-Server.log の両方に出力する
        'debug': {
            'formatter': 'debug',
            'class': 'logging.StreamHandler',
            'stream': 'ext://sys.stderr',
        },
        'debug_file': {
            'formatter': 'debug_file',
            'class': 'app.utils.LogRotation.DailyRotatingFileHandler',
            'filename': KONOMITV_SERVER_LOG_PATH,
            'encoding': 'utf-8',
            'retention_days': SERVER_LOG_ARCHIVE_RETENTION_DAYS,
        },
        # アクセスログは標準出力と server/logs/KonomiTV-BS4K-Access.log の両方に出力する
        'access': {
            'formatter': 'access',
            'class': 'logging.StreamHandler',
            'stream': 'ext://sys.stdout',
        },
        'access_file': {
            'formatter': 'access_file',
            'class': 'app.utils.LogRotation.SecureFileHandler',
            'filename': KONOMITV_ACCESS_LOG_PATH,
            'mode': 'a',
            'encoding': 'utf-8',
        },
    },
    'loggers': {
        'uvicorn': {'level': 'INFO', 'handlers': ['default', 'default_file']},
        'uvicorn.debug': {'level': 'DEBUG', 'handlers': ['debug', 'debug_file'], 'propagate': False},
        'uvicorn.access': {'level': 'INFO', 'handlers': ['access', 'access_file'], 'propagate': False},
        'uvicorn.error': {'level': 'INFO'},
    },
}

# 品質を表す Pydantic モデル
class Quality(BaseModel):
    is_hevc: bool  # 映像コーデックが HEVC かどうか
    is_60fps: bool  # フレームレートが 60fps かどうか
    width: PositiveInt  # 縦解像度
    height: PositiveInt  # 横解像度
    video_bitrate: str  # 映像のビットレート
    video_bitrate_max: str  # 映像の最大ビットレート
    audio_bitrate: str  # 音声のビットレート

# 品質の種類 (型定義)
QUALITY_TYPES = Literal[
    '4320p',
    '4320p-hevc',
    '2160p',
    '2160p-hevc',
    '1440p',
    '1440p-hevc',
    '1080p-60fps',
    '1080p-60fps-hevc',
    '1080p-30fps',
    '1080p-30fps-hevc',
    '1080p',
    '1080p-hevc',
    '810p-60fps',
    '810p-60fps-hevc',
    '810p-30fps',
    '810p-30fps-hevc',
    '810p',
    '810p-hevc',
    '720p-60fps',
    '720p-60fps-hevc',
    '720p',
    '720p-hevc',
    '720p-30fps',
    '720p-30fps-hevc',
    '540p',
    '540p-hevc',
    '540p-30fps',
    '540p-30fps-hevc',
    '480p',
    '480p-hevc',
    '480p-30fps',
    '480p-30fps-hevc',
    '360p',
    '360p-hevc',
    '360p-30fps',
    '360p-30fps-hevc',
    '240p',
    '240p-hevc',
    '240p-30fps',
    '240p-30fps-hevc',
]

# 映像と音声の品質
QUALITY: dict[QUALITY_TYPES, Quality] = {
    '4320p': Quality(
        is_hevc = False,
        is_60fps = True,
        width = 7680,
        height = 4320,
        video_bitrate = '40000K',
        video_bitrate_max = '60000K',
        audio_bitrate = '256K',
    ),
    '4320p-hevc': Quality(
        is_hevc = True,
        is_60fps = True,
        width = 7680,
        height = 4320,
        video_bitrate = '20000K',
        video_bitrate_max = '30000K',
        audio_bitrate = '192K',
    ),
    '2160p': Quality(
        is_hevc = False,
        is_60fps = True,
        width = 3840,
        height = 2160,
        video_bitrate = '18000K',
        video_bitrate_max = '25000K',
        audio_bitrate = '256K',
    ),
    '2160p-hevc': Quality(
        is_hevc = True,
        is_60fps = True,
        width = 3840,
        height = 2160,
        video_bitrate = '9000K',
        video_bitrate_max = '13500K',
        audio_bitrate = '192K',
    ),
    '1440p': Quality(
        is_hevc = False,
        is_60fps = True,
        width = 2560,
        height = 1440,
        video_bitrate = '13000K',
        video_bitrate_max = '18000K',
        audio_bitrate = '256K',
    ),
    '1440p-hevc': Quality(
        is_hevc = True,
        is_60fps = True,
        width = 2560,
        height = 1440,
        video_bitrate = '5500K',
        video_bitrate_max = '8000K',
        audio_bitrate = '192K',
    ),
    '1080p-60fps': Quality(
        is_hevc = False,
        is_60fps = True,
        width = 1440,
        height = 1080,
        video_bitrate = '11000K',
        video_bitrate_max = '15000K',
        audio_bitrate = '256K',
    ),
    '1080p-60fps-hevc': Quality(
        is_hevc = True,
        is_60fps = True,
        width = 1440,
        height = 1080,
        video_bitrate = '3500K',
        video_bitrate_max = '5200K',
        audio_bitrate = '192K',
    ),
    '1080p-30fps': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 1440,
        height = 1080,
        video_bitrate = '9500K',
        video_bitrate_max = '13000K',
        audio_bitrate = '256K',
    ),
    '1080p-30fps-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 1440,
        height = 1080,
        video_bitrate = '3000K',
        video_bitrate_max = '4500K',
        audio_bitrate = '192K',
    ),
    '1080p': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 1440,
        height = 1080,
        video_bitrate = '9500K',
        video_bitrate_max = '13000K',
        audio_bitrate = '256K',
    ),
    '1080p-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 1440,
        height = 1080,
        video_bitrate = '3000K',
        video_bitrate_max = '4500K',
        audio_bitrate = '192K',
    ),
    '810p-60fps': Quality(
        is_hevc = False,
        is_60fps = True,
        width = 1440,
        height = 810,
        video_bitrate = '6500K',
        video_bitrate_max = '9000K',
        audio_bitrate = '192K',
    ),
    '810p-60fps-hevc': Quality(
        is_hevc = True,
        is_60fps = True,
        width = 1440,
        height = 810,
        video_bitrate = '3000K',
        video_bitrate_max = '4500K',
        audio_bitrate = '192K',
    ),
    '810p-30fps': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 1440,
        height = 810,
        video_bitrate = '5500K',
        video_bitrate_max = '7600K',
        audio_bitrate = '192K',
    ),
    '810p-30fps-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 1440,
        height = 810,
        video_bitrate = '2500K',
        video_bitrate_max = '3700K',
        audio_bitrate = '192K',
    ),
    '810p': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 1440,
        height = 810,
        video_bitrate = '5500K',
        video_bitrate_max = '7600K',
        audio_bitrate = '192K',
    ),
    '810p-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 1440,
        height = 810,
        video_bitrate = '2500K',
        video_bitrate_max = '3700K',
        audio_bitrate = '192K',
    ),
    '720p-60fps': Quality(
        is_hevc = False,
        is_60fps = True,
        width = 1280,
        height = 720,
        video_bitrate = '5400K',
        video_bitrate_max = '7500K',
        audio_bitrate = '192K',
    ),
    '720p-60fps-hevc': Quality(
        is_hevc = True,
        is_60fps = True,
        width = 1280,
        height = 720,
        video_bitrate = '2400K',
        video_bitrate_max = '3600K',
        audio_bitrate = '192K',
    ),
    '720p': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 1280,
        height = 720,
        video_bitrate = '4500K',
        video_bitrate_max = '6200K',
        audio_bitrate = '192K',
    ),
    '720p-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 1280,
        height = 720,
        video_bitrate = '2000K',
        video_bitrate_max = '3000K',
        audio_bitrate = '192K',
    ),
    '720p-30fps': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 1280,
        height = 720,
        video_bitrate = '4500K',
        video_bitrate_max = '6200K',
        audio_bitrate = '192K',
    ),
    '720p-30fps-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 1280,
        height = 720,
        video_bitrate = '2000K',
        video_bitrate_max = '3000K',
        audio_bitrate = '192K',
    ),
    '540p': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 960,
        height = 540,
        video_bitrate = '3000K',
        video_bitrate_max = '4100K',
        audio_bitrate = '192K',
    ),
    '540p-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 960,
        height = 540,
        video_bitrate = '1400K',
        video_bitrate_max = '2100K',
        audio_bitrate = '192K',
    ),
    '540p-30fps': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 960,
        height = 540,
        video_bitrate = '3000K',
        video_bitrate_max = '4100K',
        audio_bitrate = '192K',
    ),
    '540p-30fps-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 960,
        height = 540,
        video_bitrate = '1400K',
        video_bitrate_max = '2100K',
        audio_bitrate = '192K',
    ),
    '480p': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 854,
        height = 480,
        video_bitrate = '2000K',
        video_bitrate_max = '2800K',
        audio_bitrate = '192K',
    ),
    '480p-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 854,
        height = 480,
        video_bitrate = '1050K',
        video_bitrate_max = '1750K',
        audio_bitrate = '192K',
    ),
    '480p-30fps': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 854,
        height = 480,
        video_bitrate = '2000K',
        video_bitrate_max = '2800K',
        audio_bitrate = '192K',
    ),
    '480p-30fps-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 854,
        height = 480,
        video_bitrate = '1050K',
        video_bitrate_max = '1750K',
        audio_bitrate = '192K',
    ),
    '360p': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 640,
        height = 360,
        video_bitrate = '1100K',
        video_bitrate_max = '1800K',
        audio_bitrate = '128K',
    ),
    '360p-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 640,
        height = 360,
        video_bitrate = '750K',
        video_bitrate_max = '1250K',
        audio_bitrate = '128K',
    ),
    '360p-30fps': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 640,
        height = 360,
        video_bitrate = '1100K',
        video_bitrate_max = '1800K',
        audio_bitrate = '128K',
    ),
    '360p-30fps-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 640,
        height = 360,
        video_bitrate = '750K',
        video_bitrate_max = '1250K',
        audio_bitrate = '128K',
    ),
    '240p': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 426,
        height = 240,
        video_bitrate = '550K',
        video_bitrate_max = '650K',
        audio_bitrate = '128K',
    ),
    '240p-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 426,
        height = 240,
        video_bitrate = '450K',
        video_bitrate_max = '650K',
        audio_bitrate = '128K',
    ),
    '240p-30fps': Quality(
        is_hevc = False,
        is_60fps = False,
        width = 426,
        height = 240,
        video_bitrate = '550K',
        video_bitrate_max = '650K',
        audio_bitrate = '128K',
    ),
    '240p-30fps-hevc': Quality(
        is_hevc = True,
        is_60fps = False,
        width = 426,
        height = 240,
        video_bitrate = '450K',
        video_bitrate_max = '650K',
        audio_bitrate = '128K',
    ),
}

# ニコニコ OAuth の Client ID
NICONICO_OAUTH_CLIENT_ID = '4JTJdyBZLwMJwaI7'

# JWT のエンコード/デコードに使うシークレットキー
## KonomiTV-BS4K は POSIX 環境専用のため、POSIX のファイル API で安全に生成・読み込みする
JWT_SECRET_KEY_PATH = DATA_DIR / 'jwt_secret.dat'
_JWT_SECRET_HEX_LENGTH = 64
_JWT_SECRET_HEX_CHARS = frozenset('0123456789abcdefABCDEF')


def _WriteAllBytes(fd: int, data: bytes) -> None:
    """ファイルディスクリプタへバイト列全体を書き込む。

    Args:
        fd: 書き込み先のファイルディスクリプタ。
        data: 書き込むバイト列。

    Returns:
        None

    Raises:
        RuntimeError: 書き込みが進まない場合。
    """

    # os.write() は短い書き込みを返す可能性があるため、全体を書き切るまで繰り返す
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise RuntimeError('Failed to write JWT secret key file.')
        offset += written


def _ValidateJWTSecretKey(secret_key: str) -> str:
    """JWT シークレットが 64 hex 文字であることを検証する。

    Args:
        secret_key: 検証対象のシークレット文字列。

    Returns:
        検証済みのシークレット文字列。

    Raises:
        RuntimeError: 長さまたは文字種が契約と異なる場合。
    """

    # 不完全または改変された値を暗号鍵として受理しない
    if len(secret_key) != _JWT_SECRET_HEX_LENGTH:
        raise RuntimeError('JWT secret key file has invalid length.')
    if any(char not in _JWT_SECRET_HEX_CHARS for char in secret_key):
        raise RuntimeError('JWT secret key file has invalid format.')
    return secret_key


def _RemoveIncompleteJWTSecretKey(path: Path, fd: int) -> None:
    """生成中の同一ファイルだけを安全に削除する。

    Args:
        path: jwt_secret.dat のパス。
        fd: 生成したファイルのファイルディスクリプタ。

    Returns:
        None
    """

    # 別ファイルへ差し替えられていた場合に誤削除しないよう、開いている inode と一致するときだけ削除する
    try:
        descriptor_stat = os.fstat(fd)
        path_stat = os.lstat(path)
    except OSError:
        # 元の例外を隠さないため、ベストエフォートの後始末に失敗した場合は呼び出し元へ戻る
        return
    if stat.S_ISREG(path_stat.st_mode) and (
        descriptor_stat.st_dev,
        descriptor_stat.st_ino,
    ) == (
        path_stat.st_dev,
        path_stat.st_ino,
    ):
        try:
            os.unlink(path)
        except OSError:
            # 元の例外を優先し、削除失敗は次回起動時の不完全キー検査に委ねる
            return


def _LoadOrCreateJWTSecretKey(path: Path) -> str:
    """JWT シークレットキーを POSIX のファイル API で安全に生成または読み込む。

    Args:
        path: jwt_secret.dat のパス。

    Returns:
        JWT シークレットキーを表す 64 hex 文字。

    Raises:
        RuntimeError: Windows で実行された、ファイル属性が安全でない、またはキーが不正な場合。
        OSError: symlink が張られているなど、ファイルを安全に開けない場合。
    """

    # KonomiTV-BS4K は Linux / POSIX 環境専用であり、不完全な Windows 互換処理は持ち込まない
    if sys.platform == 'win32':
        raise RuntimeError('KonomiTV-BS4K does not support Windows.')

    # O_EXCL で同時初期化を直列化し、O_NOFOLLOW で最終パスの symlink を原子的に拒否する
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        is_new_file = True
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        is_new_file = False

    try:
        if is_new_file is True:
            # os.open() の mode は umask の影響を受けるため、生成した同じ FD を必ず 0600 へ固定する
            os.fchmod(fd, 0o600)
            _WriteAllBytes(fd, secrets.token_hex(32).encode('ascii'))  # 32 bytes (256 bits)
            os.lseek(fd, 0, os.SEEK_SET)
        else:
            # path ではなく開いた FD を検査し、検査後のパス差し替えによる TOCTOU を避ける
            stat_result = os.fstat(fd)
            if stat.S_ISREG(stat_result.st_mode) is False:
                raise RuntimeError('JWT secret key file must be a regular file.')
            if stat_result.st_uid != os.getuid():
                raise RuntimeError('JWT secret key file must be owned by the current user.')
            if stat_result.st_nlink != 1:
                raise RuntimeError('JWT secret key file must not have multiple hard links.')
            # S_IMODE には特殊 permission bit も含まれるため、0600 以外を残さず修復する
            if stat.S_IMODE(stat_result.st_mode) != 0o600:
                os.fchmod(fd, 0o600)

        # 同時生成に負けた側は生成側の書き込み完了を最大 1 秒待つ
        for _ in range(100):
            try:
                secret_key = os.read(fd, 1024).decode('ascii').strip()
            except UnicodeDecodeError:
                raise RuntimeError('JWT secret key file has invalid format.') from None
            if len(secret_key) == _JWT_SECRET_HEX_LENGTH:
                return _ValidateJWTSecretKey(secret_key)
            # 64 bytes 以上存在するのに 64文字として読めない場合は、待機しても正常化しない
            if os.fstat(fd).st_size >= _JWT_SECRET_HEX_LENGTH:
                raise RuntimeError('JWT secret key file has invalid length.')
            os.lseek(fd, 0, os.SEEK_SET)
            time.sleep(0.01)
        raise RuntimeError('JWT secret key file is empty or incomplete.')
    except BaseException:
        # 新規生成中の失敗だけを対象に、不完全な最終ファイルが残って永続的に起動不能になることを防ぐ
        if is_new_file is True:
            _RemoveIncompleteJWTSecretKey(path, fd)
        raise
    finally:
        os.close(fd)


## jwt_secret.dat からシークレットキーをロードする
JWT_SECRET_KEY = _LoadOrCreateJWTSecretKey(JWT_SECRET_KEY_PATH)

# 暗号化された Cookie の接頭辞
TWITTER_ACCOUNT_COOKIE_ENCRYPTION_PREFIX = 'enc:'
# Cookie の暗号化に使う Fernet の暗号化キー
TWITTER_ACCOUNT_COOKIE_FERNET_KEY = base64.urlsafe_b64encode(
    hashlib.sha256(JWT_SECRET_KEY.encode('utf-8')).digest(),
)
# Cookie の暗号化に使う Fernet のインスタンス
TWITTER_ACCOUNT_COOKIE_FERNET = Fernet(TWITTER_ACCOUNT_COOKIE_FERNET_KEY)

# 暗号化された Bluesky セッション文字列の接頭辞
BLUESKY_ACCOUNT_SESSION_ENCRYPTION_PREFIX = 'enc:'
# Bluesky セッション文字列の暗号化に使う Fernet の暗号化キー
BLUESKY_ACCOUNT_SESSION_FERNET_KEY = base64.urlsafe_b64encode(
    hashlib.sha256(f'bluesky:{JWT_SECRET_KEY}'.encode()).digest(),
)
# Bluesky セッション文字列の暗号化に使う Fernet のインスタンス
BLUESKY_ACCOUNT_SESSION_FERNET = Fernet(BLUESKY_ACCOUNT_SESSION_FERNET_KEY)

# パスワードハッシュ化のための設定
PASSWORD_CONTEXT = CryptContext(
    schemes = ['bcrypt'],
    deprecated = 'auto',
)

# 外部 API に送信するリクエストヘッダー
## KonomiTV-BS4K の User-Agent を指定
API_REQUEST_HEADERS: dict[str, str] = {
    'User-Agent': f'KonomiTV-BS4K/{BS4K_VERSION}',
}

# KonomiTV で利用する httpx.AsyncClient の設定
## httpx.AsyncClient 自体は一度使ったら再利用できないので、httpx.AsyncClient を返す関数にしている
HTTPX_CLIENT = lambda: httpx.AsyncClient(
    # KonomiTV-BS4K の User-Agent を指定
    headers = API_REQUEST_HEADERS,
    # リダイレクトを追跡する
    follow_redirects = True,
    # 3 秒応答がない場合はタイムアウトする
    timeout = 3.0,
)
