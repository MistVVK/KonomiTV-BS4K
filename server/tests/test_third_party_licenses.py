import asyncio
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, Response
from httpx import AsyncClient as HTTPXAsyncClient

from app.routers import VersionRouter


async def get_third_party_licenses(licenses_path: Path) -> Response:
    """別スレッドを起動せずASGIアプリへライセンスAPI要求を送る。"""

    VersionRouter.THIRD_PARTY_LICENSES_PATH = licenses_path
    app = FastAPI()
    app.include_router(VersionRouter.router)
    async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        return await client.get('/api/version/third-party-licenses')


def test_third_party_licenses_api_renders_markdown_as_html(tmp_path: Path) -> None:
    licenses_path = tmp_path / 'THIRD_PARTY_LICENSES.md'
    licenses_path.write_text(
        '# Licenses\n\n<!-- Internal note. -->\n\n'
        '> **重要: このDockerイメージを再配布しないでください。**\n>\n> Warning.\n\n'
        '> **Docker build target**\n>\n> - Target: `cuda12-4-amd`\n\n'
        'Paragraph with a [link](https://example.com).\n\n'
        '## Chromium 1.2.3\n\nCopyright text.\n\n```text\n## Not a section\n```\n\n'
        '## KonomiTV upstream\n\n<script>alert(1)</script>\n',
        encoding = 'utf-8',
    )
    response = asyncio.run(get_third_party_licenses(licenses_path))

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html;')
    assert '<h1>Licenses</h1>' in response.text
    assert 'Internal note.' not in response.text
    assert '<blockquote class="redistribution-warning">' in response.text
    assert '<blockquote class="docker-build-target">' in response.text
    assert '<code>cuda12-4-amd</code>' in response.text
    assert '<p>Paragraph with a <a href="https://example.com">link</a>.</p>' in response.text
    assert response.text.count('<details class="license-section">') == 2
    assert '<span class="license-section__title">Chromium 1.2.3</span>' in response.text
    assert '<span class="license-section__title">KonomiTV upstream</span>' in response.text
    assert response.text.count('<span class="license-section__open">開く</span>') == 2
    assert response.text.count('<span class="license-section__close">閉じる</span>') == 2
    assert '<code class="language-text">## Not a section\n</code>' in response.text
    assert '<script>' not in response.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text
    assert response.headers['content-security-policy'].startswith("default-src 'none'")


def test_third_party_licenses_api_returns_404_when_document_is_missing(tmp_path: Path) -> None:
    response = asyncio.run(get_third_party_licenses(tmp_path / 'missing.md'))

    assert response.status_code == 404
    assert response.json() == {'detail': 'サードパーティーライセンス文書が見つかりません。'}


def test_third_party_licenses_api_renders_bundled_web_fonts_as_table(tmp_path: Path) -> None:
    licenses_path = tmp_path / 'THIRD_PARTY_LICENSES.md'
    licenses_path.write_text(
        '# Licenses\n\n'
        '## Bundled web fonts\n\n'
        '| Work | Version | License |\n'
        '| --- | --- | --- |\n'
        '| Kosugi | 5.2.5 | OFL-1.1 |\n',
        encoding = 'utf-8',
    )
    response = asyncio.run(get_third_party_licenses(licenses_path))

    assert response.status_code == 200
    assert '<span class="license-section__title">Bundled web fonts</span>' in response.text
    assert '<table>' in response.text
    assert '<th>Work</th>' in response.text
    assert '<td>Kosugi</td>' in response.text
