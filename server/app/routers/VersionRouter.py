
import gzip
import hashlib
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from markdown_it import MarkdownIt
from markdown_it.token import Token

from app import schemas
from app.bs4k_version import pickLatestBS4KVersionFromGitHubTagNames, resolveBS4KVersion
from app.config import Config
from app.constants import HTTPX_CLIENT, THIRD_PARTY_LICENSES_PATH, VERSION
from app.utils import GetPlatformEnvironment
from app.utils.Git import GetGitCommit


# ルーター
router = APIRouter(
    tags = ['Version'],
    prefix = '/api/version',
)


# GitHub API から取得した KonomiTV-BS4K の最新バージョン (と最終更新日時)
latest_version: str | None = None
latest_version_updated_at: float = 0

# ライセンス文書は数十 MB に達するため、同じ原文をリクエストごとに Markdown 変換・gzip 圧縮し直さない。
# 原文の SHA-256、HTML、gzip 済み HTML、ETag だけを1世代保持し、文書更新時は自動的に入れ替える。
_license_document_cache: tuple[bytes, bytes, bytes, str] | None = None
_chromium_license_document_cache: tuple[bytes, bytes, bytes, str] | None = None
CHROMIUM_THIRD_PARTY_LICENSES_PATH = THIRD_PARTY_LICENSES_PATH.with_name('CHROMIUM_THIRD_PARTY_LICENSES.md')


def _render_table_open(*_: Any) -> str:
    """
    Markdown の表を、キーボード操作可能な横スクロール領域として開く。

    Returns:
        str: table_open トークンの代わりに出力する HTML
    """

    return (
        '<div class="table-scroll" role="region" tabindex="0" '
        'aria-label="表（左右にスクロールできます）"><table>\n'
    )


def _render_table_close(*_: Any) -> str:
    """
    Markdown の表と横スクロール領域を閉じる。

    Returns:
        str: table_close トークンの代わりに出力する HTML
    """

    return '</table></div>\n'


def _accepts_gzip(accept_encoding: str) -> bool:
    """
    Accept-Encoding を解釈し、gzip を明示的に拒否していないか確認する。

    Args:
        accept_encoding (str): リクエストの Accept-Encoding ヘッダー

    Returns:
        bool: gzip 圧縮レスポンスを返せる場合は True
    """

    accepted: dict[str, float] = {}
    for item in accept_encoding.lower().split(','):
        parts = [part.strip() for part in item.split(';')]
        coding = parts[0]
        if not coding:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            if parameter.startswith('q='):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        accepted[coding] = quality

    # 個別指定はワイルドカードより優先するため、gzip;q=0,*;q=1 を圧縮してはならない。
    if 'gzip' in accepted:
        return accepted['gzip'] > 0
    return accepted.get('*', 0) > 0


def _create_cached_document_response(
    request: Request,
    document: bytes,
    compressed_document: bytes,
    etag: str,
    media_type: str,
    content_security_policy: str,
    content_disposition: str | None = None,
) -> Response:
    """
    キャッシュ検証と content negotiation を適用したライセンス文書を返す。

    Args:
        request (Request): FastAPI が受け取った HTTP リクエスト
        document (bytes): UTF-8 のライセンス文書
        compressed_document (bytes): gzip 圧縮済みのライセンス文書
        etag (str): 生成済み文書の SHA-256 に基づく ETag
        media_type (str): レスポンス本文の Content-Type
        content_security_policy (str): 文書に適用する Content-Security-Policy
        content_disposition (str | None): 文書を表示する際の Content-Disposition

    Returns:
        Response: 文書本文、またはキャッシュが有効な場合は 304 レスポンス
    """

    headers = {
        'Cache-Control': 'public, max-age=0, must-revalidate',
        'Content-Security-Policy': content_security_policy,
        'ETag': etag,
        'Vary': 'Accept-Encoding',
        'X-Content-Type-Options': 'nosniff',
    }
    if content_disposition is not None:
        headers['Content-Disposition'] = content_disposition

    # 文書が更新されていない再訪問では、巨大な本文を送らず 304 だけを返す。
    if_none_match = {value.strip() for value in request.headers.get('if-none-match', '').split(',')}
    if etag in if_none_match or '*' in if_none_match:
        return Response(status_code=304, headers=headers)

    # gzip を受け入れるクライアントにだけ圧縮済み本文を返し、明示的な q=0 を尊重する。
    if _accepts_gzip(request.headers.get('accept-encoding', '')):
        headers['Content-Encoding'] = 'gzip'
        return Response(content=compressed_document, headers=headers, media_type=media_type)
    return Response(content=document, headers=headers, media_type=media_type)


def _render_collapsible_licenses(markdown: MarkdownIt, source: str) -> str:
    """
    Markdown の実 h2 から h4 までを見出し階層どおりの折りたたみ表示へ変換する。

    Args:
        markdown (MarkdownIt): 安全な HTML 生成規則を設定済みの Markdown parser
        source (str): 折りたたみ表示へ変換する Markdown 文書

    Returns:
        str: 見出し階層を入れ子の details 要素として表現した HTML
    """

    tokens = markdown.parse(source)

    # Raw HTML を許可せずに警告とビルド条件だけを専用カードとして装飾できるよう、
    # 生成文書で固定している先頭の強調テキストを基準に blockquote へ意味的なクラスを付与する
    blockquote_classes = {
        '重要: NONFREE=true でビルドした Docker イメージは再配布しないでください。': 'redistribution-warning',
        'Docker image build profile': 'docker-build-target',
    }
    for index, token in enumerate(tokens):
        if token.type != 'blockquote_open':
            continue
        for blockquote_token in tokens[index + 1:]:
            if blockquote_token.type == 'blockquote_close' and blockquote_token.level == token.level:
                break
            if blockquote_token.type != 'inline':
                continue
            blockquote_label = ''.join(
                child.content for child in (blockquote_token.children or []) if child.type in ['text', 'code_inline']
            )
            if blockquote_label in blockquote_classes:
                token.attrSet('class', blockquote_classes[blockquote_label])
            break

    def GetHeadingLevel(token: Token) -> int | None:
        """
        Markdown 見出しトークンのレベルを返す。

        Args:
            token (Token): レベルを確認する markdown-it token

        Returns:
            int | None: H1～H6 の数値、または見出しでなければ None
        """

        if token.type != 'heading_open' or re.fullmatch(r'h[1-6]', token.tag) is None:
            return None
        return int(token.tag[1])

    def RenderTokenRange(start: int, end: int, parent_heading_level: int) -> str:
        """
        指定範囲を描画し、親より深い h2～h4 を入れ子の details に変換する。

        Args:
            start (int): 描画を開始する token index
            end (int): 描画を終了する token index
            parent_heading_level (int): 呼び出し元の親見出しレベル

        Returns:
            str: 指定 token 範囲を変換した HTML
        """

        rendered: list[str] = []
        normal_tokens: list[Token] = []

        # 見出し間の通常 Markdown はまとめて標準 renderer へ渡し、表などの追加 render rule も維持する。
        def FlushNormalTokens() -> None:
            """
            保留中の通常 token を Markdown renderer で HTML へ変換する。

            Returns:
                None.
            """

            if normal_tokens:
                rendered.append(markdown.renderer.render(normal_tokens, markdown.options, {}))
                normal_tokens.clear()

        index = start
        while index < end:
            heading_level = GetHeadingLevel(tokens[index])
            if heading_level is None or heading_level not in [2, 3, 4] or heading_level <= parent_heading_level:
                normal_tokens.append(tokens[index])
                index += 1
                continue

            FlushNormalTokens()

            # markdown-it-py の ATX 見出しは heading_open / inline / heading_close の3トークンで構成される。
            # 見出し本文を raw HTML として扱わず、同じ Markdown 設定で inline 描画して安全にエスケープする。
            heading_inline = tokens[index + 1]
            heading_html = markdown.renderInline(heading_inline.content)
            body_start = index + 3

            # 現在の見出しと同レベル以上の見出しが次の兄弟または祖先となる。
            # h5 / h6 は折りたたまず現在の本文に残すため、境界として扱うのは h1～現在レベルだけに限る。
            body_end = body_start
            while body_end < end:
                boundary_level = GetHeadingLevel(tokens[body_end])
                if boundary_level is not None and boundary_level <= heading_level:
                    break
                body_end += 1

            # h2 の本文中の h3、その本文中の h4 を再帰的に描画し、Markdown の親子関係を HTML にも保つ。
            body_html = RenderTokenRange(body_start, body_end, heading_level)
            heading_tag = f'h{heading_level}'
            level_class = f'level-{heading_level}'
            rendered.append(
                f'<details class="license-section license-section--{level_class}">'
                f'<summary class="license-section__summary license-section__summary--{level_class}">'
                f'<{heading_tag} class="license-section__heading license-section__heading--{level_class}">'
                '<span class="license-section__title">' + heading_html + '</span>'
                '<span class="license-section__state" aria-hidden="true">'
                '<span class="license-section__open">開く</span>'
                f'<span class="license-section__close">閉じる</span></span></{heading_tag}></summary>'
                f'<div class="license-section__body license-section__body--{level_class}">' + body_html + '</div>'
                '</details>'
            )
            index = body_end

        FlushNormalTokens()
        return ''.join(rendered)

    return RenderTokenRange(0, len(tokens), 1)


@router.get(
    '/third-party-licenses',
    summary = 'サードパーティーライセンス取得 API',
    response_class = HTMLResponse,
)
async def ThirdPartyLicensesAPI(request: Request) -> Response:
    """
    KonomiTV-BS4K に同梱している third-party ソフトウェアのライセンス全文を返す。

    Returns:
        Response: HTML 形式のライセンス文書
    """

    global _license_document_cache

    try:
        licenses_source = THIRD_PARTY_LICENSES_PATH.read_bytes()
    except FileNotFoundError as ex:
        raise HTTPException(status_code = 404, detail = 'サードパーティーライセンス文書が見つかりません。') from ex

    # ファイル時刻ではなく原文の SHA-256 でキャッシュを判定し、同一時刻・同一サイズの更新も取りこぼさない。
    source_digest = hashlib.sha256(licenses_source).digest()
    if _license_document_cache is not None and _license_document_cache[0] == source_digest:
        _, cached_document, cached_compressed_document, cached_etag = _license_document_cache
        return _create_cached_document_response(
            request,
            cached_document,
            cached_compressed_document,
            cached_etag,
            'text/html; charset=utf-8',
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'",
        )
    licenses_markdown = licenses_source.decode('utf-8')

    # ライセンス文書はビルド時に生成されるが、意図しない HTML が実行されないよう Raw HTML は無効化する
    markdown = MarkdownIt('commonmark', {'html': False, 'linkify': False, 'typographer': False}).enable('table')
    markdown.add_render_rule('table_open', _render_table_open)
    markdown.add_render_rule('table_close', _render_table_close)
    # Raw HTML を有効化せず、生成文書内の説明用 HTML コメントだけを表示対象から除外する
    licenses_markdown = re.sub(r'<!--.*?-->', '', licenses_markdown, flags = re.DOTALL)
    licenses_html = _render_collapsible_licenses(markdown, licenses_markdown)

    document = f'''<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>サードパーティーソフトウェアのライセンス</title>
    <style>
        :root {{ color-scheme: dark; }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            background: #0d1117;
            color: #e6edf3;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            font-size: 15px;
            line-height: 1.7;
            overflow-wrap: anywhere;
        }}
        main {{ width: min(100% - 32px, 960px); margin: 0 auto; padding: 40px 0 64px; }}
        h1, h2, h3, h4, h5 {{ margin: 1.5em 0 0.6em; line-height: 1.35; }}
        h1 {{ margin-top: 0; font-size: 2rem; }}
        h2 {{ padding-bottom: 0.3em; border-bottom: 1px solid #30363d; font-size: 1.45rem; }}
        h3 {{ font-size: 1.15rem; }}
        h4 {{ font-size: 1.05rem; }}
        h5 {{ font-size: 1rem; }}
        p, pre {{ margin: 0.8em 0; }}
        a {{ color: #58a6ff; }}
        hr {{ height: 1px; margin: 2em 0; border: 0; background: #30363d; }}
        code, pre {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
        code {{ padding: 0.15em 0.35em; border-radius: 4px; background: #161b22; }}
        pre {{ max-width: 100%; padding: 16px; overflow-x: auto; border-radius: 6px; background: #161b22; }}
        pre code {{ padding: 0; background: transparent; overflow-wrap: normal; }}
        .table-scroll {{ width: 100%; margin: 0.8em 0; overflow-x: auto; overscroll-behavior-inline: contain; }}
        .table-scroll:focus-visible {{ outline: 2px solid #58a6ff; outline-offset: 2px; }}
        table {{ width: max-content; min-width: 640px; border-collapse: collapse; }}
        th, td {{
            padding: 8px 10px; border: 1px solid #30363d; text-align: left; vertical-align: top;
            overflow-wrap: normal; word-break: normal;
        }}
        th {{ background: #21262d; }}
        blockquote {{ margin: 1em 0; padding-left: 1em; border-left: 3px solid #30363d; color: #b1bac4; }}
        main > blockquote.redistribution-warning {{
            padding: 14px 16px;
            border: 2px solid #f85149;
            border-radius: 8px;
            background: #3d1418;
            color: #ffdcd7;
            font-weight: 700;
        }}
        main > blockquote.redistribution-warning p {{ margin: 0.35em 0; }}
        main > blockquote.docker-build-target {{
            padding: 14px 16px;
            border: 1px solid #388bfd;
            border-radius: 8px;
            background: #111d2e;
            color: #c9d1d9;
        }}
        main > blockquote.docker-build-target p {{ margin: 0.35em 0; font-weight: 700; }}
        main > blockquote.docker-build-target ul {{ margin: 0.5em 0 0; padding-left: 1.5em; }}
        .license-section {{
            margin: 12px 0; border: 1px solid #30363d; border-radius: 8px; background: #161b22;
        }}
        .license-section--level-3, .license-section--level-4 {{
            margin: 10px 0 0 12px;
            background: #0d1117;
        }}
        .license-section--level-4 {{ border-color: #21262d; background: #161b22; }}
        .license-section__summary {{
            padding: 16px 18px; cursor: pointer; list-style: none; font-weight: 700;
        }}
        .license-section__summary--level-3, .license-section__summary--level-4 {{ padding: 13px 15px; }}
        .license-section__summary::-webkit-details-marker {{ display: none; }}
        .license-section__summary:focus-visible {{ outline: 2px solid #58a6ff; outline-offset: 2px; }}
        .license-section__heading {{
            display: flex; align-items: center; gap: 16px; min-width: 0; margin: 0; padding: 0;
            border: 0; line-height: inherit;
        }}
        .license-section__title {{ min-width: 0; flex: 1; font-size: 1.15rem; overflow-wrap: anywhere; }}
        .license-section__heading--level-3 .license-section__title {{ font-size: 1.05rem; }}
        .license-section__heading--level-4 .license-section__title {{ font-size: 1rem; }}
        .license-section__state {{ flex: none; }}
        .license-section__open, .license-section__close {{ flex: none; color: #58a6ff; font-size: 0.9rem; }}
        .license-section__close,
        .license-section[open] > .license-section__summary .license-section__open {{ display: none; }}
        .license-section[open] > .license-section__summary .license-section__close {{ display: inline; }}
        .license-section__body {{ padding: 0 18px 18px; border-top: 1px solid #30363d; }}
        .license-section__body--level-3, .license-section__body--level-4 {{ padding: 0 15px 15px; }}
        .license-section__body > :first-child {{ margin-top: 16px; }}
        @media (max-width: 600px) {{
            body {{ font-size: 14px; }}
            main {{ width: min(100% - 24px, 960px); padding: 24px 0 40px; }}
            h1 {{ font-size: 1.55rem; }}
            h2 {{ font-size: 1.25rem; }}
            .license-section--level-3, .license-section--level-4 {{ margin-left: 6px; }}
            .license-section__summary {{ padding: 14px; }}
            .license-section__summary--level-3, .license-section__summary--level-4 {{ padding: 12px; }}
            .license-section__body {{ padding: 0 14px 14px; }}
            .license-section__body--level-3, .license-section__body--level-4 {{ padding: 0 12px 12px; }}
        }}
    </style>
</head>
<body>
    <main>{licenses_html}</main>
</body>
</html>'''

    document_bytes = document.encode('utf-8')
    compressed_document = gzip.compress(document_bytes, compresslevel=6, mtime=0)
    # Content-Encoding の有無で転送バイト列は変わるため、意味的に同じ文書を示す weak ETag を使う。
    etag = f'W/"{hashlib.sha256(document_bytes).hexdigest()}"'
    _license_document_cache = (source_digest, document_bytes, compressed_document, etag)
    return _create_cached_document_response(
        request,
        document_bytes,
        compressed_document,
        etag,
        'text/html; charset=utf-8',
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'",
    )


@router.get(
    '/chromium-third-party-licenses',
    summary = 'Chromium サードパーティーライセンス取得 API',
    response_class = Response,
)
async def ChromiumThirdPartyLicensesAPI(request: Request) -> Response:
    """
    Chromium が同梱する多数のコンポーネントのライセンス原文を Markdown 形式で返す。

    Args:
        request (Request): FastAPI が受け取った HTTP リクエスト

    Returns:
        Response: Chromium のライセンス文書、またはキャッシュが有効な場合は 304 レスポンス
    """

    global _chromium_license_document_cache

    try:
        document = CHROMIUM_THIRD_PARTY_LICENSES_PATH.read_bytes()
    except FileNotFoundError as ex:
        raise HTTPException(status_code=404, detail='Chromium のサードパーティーライセンス文書が見つかりません。') from ex

    # 765件規模の Markdown を巨大な HTML DOM へ変換せず、原文のまま gzip と HTTP キャッシュを適用する。
    source_digest = hashlib.sha256(document).digest()
    if _chromium_license_document_cache is not None and _chromium_license_document_cache[0] == source_digest:
        _, cached_document, cached_compressed_document, cached_etag = _chromium_license_document_cache
        return _create_cached_document_response(
            request,
            cached_document,
            cached_compressed_document,
            cached_etag,
            'text/markdown; charset=utf-8',
            "default-src 'none'; base-uri 'none'; form-action 'none'",
            'inline; filename="CHROMIUM_THIRD_PARTY_LICENSES.md"',
        )

    compressed_document = gzip.compress(document, compresslevel=6, mtime=0)
    etag = f'W/"{source_digest.hex()}"'
    _chromium_license_document_cache = (source_digest, document, compressed_document, etag)
    return _create_cached_document_response(
        request,
        document,
        compressed_document,
        etag,
        'text/markdown; charset=utf-8',
        "default-src 'none'; base-uri 'none'; form-action 'none'",
        'inline; filename="CHROMIUM_THIRD_PARTY_LICENSES.md"',
    )


@router.get(
    '',
    summary = 'バージョン情報取得 API',
    response_description = 'KonomiTV-BS4K サーバーのバージョンなどの情報。',
    response_model = schemas.VersionInformation,
)
async def VersionInformationAPI():
    """
    KonomiTV-BS4K サーバーのバージョン情報と、バックエンドの種類、稼働環境などを取得する。
    """

    global latest_version, latest_version_updated_at

    # GitHub API で KonomiTV-BS4K の最新のタグ (=最新バージョン) を取得
    ## GitHub API は無認証だと60回/1時間までしかリクエストできないので、リクエスト結果を10分ほどキャッシュする
    ## タグが0件の場合も latest_version=None を正常な取得結果としてキャッシュする
    ## upstream の v0.x タグと混在するため、bs4k-v* だけを semver 最大で選ぶ
    if latest_version_updated_at == 0 or (time.time() - latest_version_updated_at) > 60 * 10:
        try:
            async with HTTPX_CLIENT() as client:
                response = await client.get('https://api.github.com/repos/MistVVK/KonomiTV-BS4K/tags')
            if response.status_code == 200:
                tags = response.json()
                tag_names = [
                    str(tag.get('name', ''))
                    for tag in tags
                    if isinstance(tag, dict)
                ]
                latest_version = pickLatestBS4KVersionFromGitHubTagNames(tag_names)
                latest_version_updated_at = time.time()
        except (httpx.NetworkError, httpx.TimeoutException):
            pass

    # サーバーが稼働している環境を取得
    environment = GetPlatformEnvironment()

    # 起動後にタグが付いた開発ツリーでも、API では都度キャッシュ済みの解決結果を返す
    # （プロセス内キャッシュ。force はテスト以外では使わない）
    bs4k_version = resolveBS4KVersion()

    general = Config().general
    result: dict[str, Any] = {
        'version': bs4k_version,
        'upstream_version': VERSION,
        'git_commit': await GetGitCommit(),
        'latest_version': latest_version,
        'environment': environment,
        'backend': general.backend,
        'encoder': general.encoder,
        # フル設定 GET から分離した、視聴経路向けの非機密 runtime 情報
        'encoder_bs4k': general.encoder_bs4k,
        'bs4k_ignore_viewer_low_latency': general.bs4k_ignore_viewer_low_latency,
        # 保存直後の設定値ではなく、このプロセスで実際に有効な実況機能の状態を返す
        'jikkyo_enabled': general.jikkyo_enabled,
    }
    return result
