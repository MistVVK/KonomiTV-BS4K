import asyncio
from pathlib import Path

from bs4 import BeautifulSoup
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
        '> **重要: `NONFREE=true` でビルドした Docker イメージは再配布しないでください。**\n>\n> Warning.\n\n'
        '> **Docker image build profile**\n>\n> - Profile: `cuda12.4-nonfree`\n\n'
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
    assert '<code>cuda12.4-nonfree</code>' in response.text
    assert '<p>Paragraph with a <a href="https://example.com">link</a>.</p>' in response.text
    assert response.text.count('<details class="license-section license-section--level-2">') == 2
    assert response.text.count(
        '<summary class="license-section__summary license-section__summary--level-2">'
        '<h2 class="license-section__heading license-section__heading--level-2">'
    ) == 2
    assert '<span class="license-section__title">Chromium 1.2.3</span>' in response.text
    assert '<span class="license-section__title">KonomiTV upstream</span>' in response.text
    assert response.text.count('<span class="license-section__open">開く</span>') == 2
    assert response.text.count('<span class="license-section__close">閉じる</span>') == 2
    assert '<code class="language-text">## Not a section\n</code>' in response.text
    assert '<script>' not in response.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text
    assert response.headers['content-security-policy'].startswith("default-src 'none'")


def test_third_party_licenses_api_preserves_nested_heading_hierarchy(tmp_path: Path) -> None:
    licenses_path = tmp_path / 'THIRD_PARTY_LICENSES.md'
    licenses_path.write_text(
        '# Licenses\n\n'
        '## Client\n\nClient introduction.\n\n'
        '### Bundled assets\n\nAsset introduction.\n\n'
        '#### Web fonts\n\nFont introduction.\n\n'
        '##### Font license text\n\nThe h5 remains normal content.\n\n'
        '````text\n## Fenced h2\n### Fenced h3\n#### Fenced h4\n````\n\n'
        '### JavaScript packages\n\n'
        '#### Package A\n\nPackage A license.\n\n'
        '#### Package B\n\nPackage B license.\n\n'
        '## Final runtime\n\n'
        '### OS packages\n\n'
        '#### libc\n\nlibc license.\n',
        encoding = 'utf-8',
    )
    response = asyncio.run(get_third_party_licenses(licenses_path))

    assert response.status_code == 200
    document = BeautifulSoup(response.text, 'html.parser')
    main = document.find('main')
    assert main is not None

    # h2 は main 直下の兄弟、h3 と h4 はそれぞれ親見出しの本文へ直接入れ子になる。
    level2_sections = main.find_all('details', class_='license-section--level-2', recursive=False)
    assert [section.find(class_='license-section__title').get_text(strip=True) for section in level2_sections] == [
        'Client',
        'Final runtime',
    ]
    client_body = level2_sections[0].find('div', class_='license-section__body--level-2', recursive=False)
    assert client_body is not None
    client_level3_sections = client_body.find_all(
        'details',
        class_='license-section--level-3',
        recursive=False,
    )
    assert [section.find(class_='license-section__title').get_text(strip=True) for section in client_level3_sections] == [
        'Bundled assets',
        'JavaScript packages',
    ]

    assets_body = client_level3_sections[0].find('div', class_='license-section__body--level-3', recursive=False)
    assert assets_body is not None
    asset_level4_sections = assets_body.find_all(
        'details',
        class_='license-section--level-4',
        recursive=False,
    )
    assert [section.find(class_='license-section__title').get_text(strip=True) for section in asset_level4_sections] == [
        'Web fonts',
    ]

    javascript_body = client_level3_sections[1].find(
        'div',
        class_='license-section__body--level-3',
        recursive=False,
    )
    assert javascript_body is not None
    javascript_level4_sections = javascript_body.find_all(
        'details',
        class_='license-section--level-4',
        recursive=False,
    )
    assert [section.find(class_='license-section__title').get_text(strip=True) for section in javascript_level4_sections] == [
        'Package A',
        'Package B',
    ]

    # summary は元の見出しレベルを保ち、視覚用の開閉ラベルは読み上げ対象から除外する。
    for heading_level in [2, 3, 4]:
        for section in document.select(f'details.license-section--level-{heading_level}'):
            summary = section.find('summary', recursive=False)
            assert summary is not None
            assert f'license-section__summary--level-{heading_level}' in summary.get('class', [])
            heading = summary.find(f'h{heading_level}', recursive=False)
            assert heading is not None
            assert f'license-section__heading--level-{heading_level}' in heading.get('class', [])
            state = heading.find('span', class_='license-section__state', recursive=False)
            assert state is not None
            assert state.get('aria-hidden') == 'true'
            assert state.find(class_='license-section__open').get_text(strip=True) == '開く'
            assert state.find(class_='license-section__close').get_text(strip=True) == '閉じる'

    # h5 とコードフェンス内の見出し風テキストは details に変換しない。
    assert document.find('h5', string='Font license text') is not None
    assert len(document.select('details.license-section')) == 9
    assert not any(
        'Fenced h' in title.get_text()
        for title in document.select('.license-section__title')
    )
    fenced_code = document.find('code', class_='language-text')
    assert fenced_code is not None
    assert '## Fenced h2\n### Fenced h3\n#### Fenced h4' in fenced_code.get_text()


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
