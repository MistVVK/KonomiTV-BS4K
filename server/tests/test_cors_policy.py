"""本線 API の CORS ポリシーの現行契約を検証する (監査 F-20 / 改修ブロック R-16 の判断記録)。

実在する cross-origin 配置を列挙した結果:
- 本番の KonomiTV-BS4K Web UI はサーバー自身から配信される同一オリジン (例: https://my.local.konomi.tv:7000) であり、
  ブラウザは CORS を適用しない
- 開発環境のクライアント開発サーバー (port 7001 → API port 7000) は cross-origin だが、
  開発時は config.yaml の general.debug = true により全オリジン許可 (Origin 反射) となるため動作する
- KomorebiV1 互換 API は別ポートの別アプリケーションで CORSMiddleware を持たず、
  ブラウザからの cross-origin アクセスはもともと拒否される (ネイティブクライアントには CORS は適用されない)
- https://app.konomi.tv からの接続のみ本番で許容する upstream 由来の契約は、
  ニコニコ実況 OAuth のリダイレクト中継などの連携のために維持する
以上より「本番で my.local.konomi.tv からのリクエストが弾かれる」配置は実在せず、
CORS 設定の挙動変更は行わない。本テストはその判断根拠として現行契約を固定する。
"""

import asyncio
import importlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient
from starlette.requests import Request

from app import config as config_module
from app.CompatibilityAPI import CreateCompatibilityAPI


# app.app は通常 KonomiTV.py で初期化済みの設定を参照するため、単体テストでは安全な既定値を先に設定する。
# 既定値は general.debug = False なので、本番相当の CORS ポリシー (https://app.konomi.tv のみ許可) が適用される。
if config_module._CONFIG is None:
    config_module._CONFIG = config_module.HostServerSettings().toServerSettings(bypass_validation=True)
app_module = importlib.import_module('app.app')

# テストプロセス終了時の atexit fallback が実サービス用 cleanup を起動しないよう、既定状態を完了済みにする。
app_module._shutdown_completed = True
main_app = app_module.app

# 本番で許可される唯一のオリジン (upstream 由来の契約)
_ALLOWED_ORIGIN = 'https://app.konomi.tv'
# 許可されないオリジンの代表例
_DISALLOWED_ORIGIN = 'https://evil.example.com'
# 開発環境のクライアント開発サーバーのオリジンの代表例
_DEV_SERVER_ORIGIN = 'https://my.local.konomi.tv:7001'


async def _FetchCORSResponses() -> tuple:
    """許可/不許可オリジンからの preflight・実リクエスト・同一オリジン相当リクエストをまとめて取得する。"""

    async with HTTPXAsyncClient(transport=ASGITransport(app=main_app), base_url='http://test') as client:
        preflight_allowed = await client.options('/api/version', headers={
            'Origin': _ALLOWED_ORIGIN,
            'Access-Control-Request-Method': 'GET',
        })
        preflight_disallowed = await client.options('/api/version', headers={
            'Origin': _DISALLOWED_ORIGIN,
            'Access-Control-Request-Method': 'GET',
        })
        # 不許可オリジンからの単純リクエスト (preflight を伴わない GET) は 404 応答でも CORS ヘッダーが付与されない
        get_disallowed = await client.get('/api/nonexistent-cors-probe', headers={'Origin': _DISALLOWED_ORIGIN})
        # 同一オリジンからのリクエストには Origin ヘッダーが付かず、CORS ヘッダーも不要
        get_same_origin = await client.get('/api/nonexistent-cors-probe')
        return preflight_allowed, preflight_disallowed, get_disallowed, get_same_origin


def test_cors_preflight_allows_only_app_konomi_tv() -> None:
    """preflight は https://app.konomi.tv のみ許可し、それ以外のオリジンには許可ヘッダーを返さない。"""

    preflight_allowed, preflight_disallowed, _, _ = asyncio.run(_FetchCORSResponses())

    # 許可されたオリジンには Origin がそのまま返る (allow_credentials=True のため * ではなく明示される)
    assert preflight_allowed.status_code == 200
    assert preflight_allowed.headers.get('access-control-allow-origin') == _ALLOWED_ORIGIN
    assert preflight_allowed.headers.get('access-control-allow-credentials') == 'true'

    # 不許可のオリジンには Access-Control-Allow-Origin ヘッダー自体が返らない
    ## (* を含むいかなる値も返らないことを、ヘッダー不在で検証する。ブラウザはこの応答でリクエストをブロックする)
    assert 'access-control-allow-origin' not in preflight_disallowed.headers


def test_cors_actual_request_from_disallowed_origin_has_no_allow_header() -> None:
    """不許可オリジンからの実リクエストには Access-Control-Allow-Origin が付与されない。"""

    _, _, get_disallowed, get_same_origin = asyncio.run(_FetchCORSResponses())

    # リクエスト自体は処理されるが、ブラウザは ACAO 欠落によりレスポンスを JavaScript へ渡さない
    assert get_disallowed.status_code == 404
    assert 'access-control-allow-origin' not in get_disallowed.headers

    # 同一オリジン相当 (Origin ヘッダーなし) のリクエストは CORS を介さず通常通り処理される
    assert get_same_origin.status_code == 404
    assert 'access-control-allow-origin' not in get_same_origin.headers


def test_debug_cors_middleware_reflects_development_server_origin() -> None:
    """debug 時の実 CORSMiddleware は開発サーバーからの preflight に Origin を反射する。"""

    # app.app の CORS ミドルウェアは import 時の debug 値で構築されるため、現在の本番相当 app を後から変更しない
    ## 隔離プロセスで debug=True を設定してから実際の app.app を import し、本番と同じ初期化経路へ OPTIONS を送る
    script = textwrap.dedent(f'''
        import asyncio
        import importlib
        import json

        from httpx import ASGITransport
        from httpx import AsyncClient as HTTPXAsyncClient

        from app import config as config_module

        settings = config_module.HostServerSettings().toServerSettings(bypass_validation=True)
        settings.general.debug = True
        config_module._CONFIG = settings
        app_module = importlib.import_module('app.app')
        app_module._shutdown_completed = True

        async def FetchPreflightResponse():
            async with HTTPXAsyncClient(transport=ASGITransport(app=app_module.app), base_url='http://test') as client:
                return await client.options('/api/version', headers={{
                    'Origin': '{_DEV_SERVER_ORIGIN}',
                    'Access-Control-Request-Method': 'GET',
                    'Access-Control-Request-Headers': 'Authorization',
                }})

        response = asyncio.run(FetchPreflightResponse())
        print(json.dumps({{
            'cors_origins': app_module.CORS_ORIGINS,
            'status_code': response.status_code,
            'allow_origin': response.headers.get('access-control-allow-origin'),
            'allow_credentials': response.headers.get('access-control-allow-credentials'),
        }}))
    ''')
    completed_process = subprocess.run(
        [sys.executable, '-c', script],
        cwd = Path(__file__).resolve().parents[1],
        capture_output = True,
        text = True,
        check = True,
    )
    result = json.loads(completed_process.stdout.splitlines()[-1])

    # debug ポリシーは全オリジン許可で、credentials を許可する preflight では要求元 Origin が明示される
    assert result == {
        'cors_origins': ['*'],
        'status_code': 200,
        'allow_origin': _DEV_SERVER_ORIGIN,
        'allow_credentials': 'true',
    }


def _MakeRequestWithOrigin(origin: str) -> Request:
    """指定した Origin ヘッダーを持つ最小限の Request を生成する。"""

    return Request({
        'type': 'http',
        'method': 'GET',
        'path': '/',
        'headers': [(b'origin', origin.encode())],
        'query_string': b'',
        'server': ('test', 443),
    })


def test_exception_handler_cors_header_reflects_origin_only_in_debug_mode() -> None:
    """例外ハンドラ用の CORS ヘッダーは debug 時のみ任意の Origin を反射し、本番では許可リスト外を返さない。

    開発環境のクライアント開発サーバー (port 7001) からの cross-origin リクエストは
    general.debug = true での Origin 反射によって成立している、という判断根拠を固定する。
    """

    request = _MakeRequestWithOrigin(_DEV_SERVER_ORIGIN)
    original_debug = app_module.CONFIG.general.debug
    try:
        # debug 時は任意の Origin がそのまま反射される (allow_origins=['*'] + credentials のため * ではなく反射が必要)
        app_module.CONFIG.general.debug = True
        assert app_module.GetCORSHeadersForExceptionHandler(request) == {'Access-Control-Allow-Origin': _DEV_SERVER_ORIGIN}

        # 本番 (debug=False) では許可リスト外の Origin には CORS ヘッダーを返さない
        app_module.CONFIG.general.debug = False
        assert app_module.GetCORSHeadersForExceptionHandler(request) == {}
        # 本番でも許可リスト内の Origin にはその Origin を返す
        assert app_module.GetCORSHeadersForExceptionHandler(_MakeRequestWithOrigin(_ALLOWED_ORIGIN)) == {
            'Access-Control-Allow-Origin': _ALLOWED_ORIGIN,
        }
    finally:
        app_module.CONFIG.general.debug = original_debug


def test_compatibility_api_has_no_cors_middleware() -> None:
    """KomorebiV1 互換 API は CORSMiddleware を持たず、ブラウザからの cross-origin アクセスを許可しない。

    互換 API はネイティブクライアント (CORS が適用されない) からの利用を前提とする契約を固定する。
    """

    compatibility_app = CreateCompatibilityAPI()
    middleware_classes = [middleware.cls for middleware in compatibility_app.user_middleware]
    assert CORSMiddleware not in middleware_classes
