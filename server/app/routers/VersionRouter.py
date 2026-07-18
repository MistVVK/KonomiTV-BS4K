
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from markdown_it import MarkdownIt
from markdown_it.token import Token

from app import schemas
from app.config import Config
from app.constants import HTTPX_CLIENT, THIRD_PARTY_LICENSES_PATH, VERSION
from app.utils import GetPlatformEnvironment
from app.utils.Git import GetGitCommit


# ルーター
router = APIRouter(
    tags = ['Version'],
    prefix = '/api/version',
)


# GitHub API から取得した KonomiTV の最新バージョン (と最終更新日時)
latest_version: str | None = None
latest_version_updated_at: float = 0


def _render_collapsible_licenses(markdown: MarkdownIt, source: str) -> str:
    """Markdown の実 h2 ごとにライセンス本文を折りたたみ表示へ変換する。"""

    tokens = markdown.parse(source)

    # Raw HTML を許可せずに警告とビルド条件だけを専用カードとして装飾できるよう、
    # 生成文書で固定している先頭の強調テキストを基準に blockquote へ意味的なクラスを付与する
    blockquote_classes = {
        '重要: このDockerイメージを再配布しないでください。': 'redistribution-warning',
        'Docker build target': 'docker-build-target',
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
                child.content for child in (blockquote_token.children or []) if child.type == 'text'
            )
            if blockquote_label in blockquote_classes:
                token.attrSet('class', blockquote_classes[blockquote_label])
            break

    rendered: list[str] = []
    normal_tokens: list[Token] = []

    def flush_normal_tokens() -> None:
        if normal_tokens:
            rendered.append(markdown.renderer.render(normal_tokens, markdown.options, {}))
            normal_tokens.clear()

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type != 'heading_open' or token.tag != 'h2':
            normal_tokens.append(token)
            index += 1
            continue

        flush_normal_tokens()
        heading_inline = tokens[index + 1]
        heading_html = markdown.renderInline(heading_inline.content)
        body_start = index + 3
        body_end = body_start
        while body_end < len(tokens):
            boundary = tokens[body_end]
            if boundary.type == 'heading_open' and boundary.tag in ['h1', 'h2']:
                break
            body_end += 1
        body_html = markdown.renderer.render(tokens[body_start:body_end], markdown.options, {})
        rendered.append(
            '<details class="license-section">'
            '<summary><span class="license-section__title">' + heading_html + '</span>'
            '<span class="license-section__open">開く</span>'
            '<span class="license-section__close">閉じる</span></summary>'
            '<div class="license-section__body">' + body_html + '</div>'
            '</details>'
        )
        index = body_end

    flush_normal_tokens()
    return ''.join(rendered)


@router.get(
    '/third-party-licenses',
    summary = 'サードパーティーライセンス取得 API',
    response_class = HTMLResponse,
)
def ThirdPartyLicensesAPI() -> HTMLResponse:
    """
    KonomiTV に同梱している third-party ソフトウェアのライセンス全文を返す。

    Returns:
        HTMLResponse: HTML 形式のライセンス文書
    """

    try:
        licenses_markdown = THIRD_PARTY_LICENSES_PATH.read_text(encoding = 'utf-8')
    except FileNotFoundError as ex:
        raise HTTPException(status_code = 404, detail = 'サードパーティーライセンス文書が見つかりません。') from ex

    # ライセンス文書はビルド時に生成されるが、意図しない HTML が実行されないよう Raw HTML は無効化する
    markdown = MarkdownIt('commonmark', {'html': False, 'linkify': False, 'typographer': False}).enable('table')
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
        h1, h2, h3 {{ margin: 1.5em 0 0.6em; line-height: 1.35; }}
        h1 {{ margin-top: 0; font-size: 2rem; }}
        h2 {{ padding-bottom: 0.3em; border-bottom: 1px solid #30363d; font-size: 1.45rem; }}
        h3 {{ font-size: 1.15rem; }}
        p, pre {{ margin: 0.8em 0; }}
        a {{ color: #58a6ff; }}
        hr {{ height: 1px; margin: 2em 0; border: 0; background: #30363d; }}
        code, pre {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
        code {{ padding: 0.15em 0.35em; border-radius: 4px; background: #161b22; }}
        pre {{ max-width: 100%; padding: 16px; overflow-x: auto; border-radius: 6px; background: #161b22; }}
        pre code {{ padding: 0; background: transparent; overflow-wrap: normal; }}
        table {{ display: block; width: 100%; overflow-x: auto; border-collapse: collapse; }}
        th, td {{ padding: 8px 10px; border: 1px solid #30363d; text-align: left; vertical-align: top; }}
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
        .license-section {{ margin: 12px 0; border: 1px solid #30363d; border-radius: 8px; background: #161b22; }}
        .license-section summary {{
            display: flex; align-items: center; gap: 16px; padding: 16px 18px; cursor: pointer;
            list-style: none; font-weight: 700;
        }}
        .license-section summary::-webkit-details-marker {{ display: none; }}
        .license-section__title {{ min-width: 0; flex: 1; font-size: 1.15rem; overflow-wrap: anywhere; }}
        .license-section__open, .license-section__close {{ flex: none; color: #58a6ff; font-size: 0.9rem; }}
        .license-section__close, .license-section[open] .license-section__open {{ display: none; }}
        .license-section[open] .license-section__close {{ display: inline; }}
        .license-section__body {{ padding: 0 18px 18px; border-top: 1px solid #30363d; }}
        .license-section__body > :first-child {{ margin-top: 16px; }}
        @media (max-width: 600px) {{
            body {{ font-size: 14px; }}
            main {{ width: min(100% - 24px, 960px); padding: 24px 0 40px; }}
            h1 {{ font-size: 1.55rem; }}
            h2 {{ font-size: 1.25rem; }}
            .license-section summary {{ padding: 14px; }}
            .license-section__body {{ padding: 0 14px 14px; }}
        }}
    </style>
</head>
<body>
    <main>{licenses_html}</main>
</body>
</html>'''

    return HTMLResponse(
        content = document,
        headers = {
            'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'",
            'X-Content-Type-Options': 'nosniff',
        },
    )

@router.get(
    '',
    summary = 'バージョン情報取得 API',
    response_description = 'KonomiTV サーバーのバージョンなどの情報。',
    response_model = schemas.VersionInformation,
)
async def VersionInformationAPI():
    """
    KonomiTV サーバーのバージョン情報と、バックエンドの種類、稼働環境などを取得する。
    """

    global latest_version, latest_version_updated_at

    # GitHub API で KonomiTV の最新のタグ (=最新バージョン) を取得
    ## GitHub API は無認証だと60回/1時間までしかリクエストできないので、リクエスト結果を10分ほどキャッシュする
    if latest_version is None or (time.time() - latest_version_updated_at) > 60 * 10:
        try:
            async with HTTPX_CLIENT() as client:
                response = await client.get('https://api.github.com/repos/tsukumijima/KonomiTV/tags')
            if response.status_code == 200:
                latest_version = response.json()[0]['name'].replace('v', '')  # 先頭の v を取り除く
                latest_version_updated_at = time.time()
        except (httpx.NetworkError, httpx.TimeoutException):
            pass

    # サーバーが稼働している環境を取得
    environment = GetPlatformEnvironment()

    result: dict[str, Any] = {
        'version': VERSION,
        'git_commit': await GetGitCommit(),
        'latest_version': latest_version,
        'environment': environment,
        'backend': Config().general.backend,
        'encoder': Config().general.encoder,
    }
    return result
