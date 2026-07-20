import asyncio
import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient
from markdown_it import MarkdownIt

from app.routers import VersionRouter


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR_PATH = REPOSITORY_ROOT / 'docker/thirdparty/collect-license-manifest.py'


def RunCollector(tmp_path: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """テスト用の最小構成でライセンス manifest collector を実行する。"""

    return subprocess.run(
        [
            sys.executable,
            str(COLLECTOR_PATH),
            '--stage',
            'test',
            '--output',
            str(tmp_path / 'licenses.md'),
            *arguments,
        ],
        check=check,
        capture_output=True,
        text=True,
    )


def CreatePythonMetadata(root: Path, *, name: str, version: str, extra: str = '') -> None:
    """ライセンスファイルを持たない最小 dist-info を作成する。"""

    metadata_path = root / f'{name}-{version}.dist-info/METADATA'
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        f'Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nLicense: MIT\n{extra}',
        encoding='utf-8',
    )


def test_collector_identifies_runtime_paths_and_deduplicates_exact_content(tmp_path: Path) -> None:
    first_root = tmp_path / 'runtime-a'
    second_root = tmp_path / 'runtime-b'
    first_license = first_root / 'component-one/LICENSE'
    second_license = second_root / 'nested/component-two/LICENSE.txt'
    first_license.parent.mkdir(parents=True)
    second_license.parent.mkdir(parents=True)
    license_text = 'Copyright (c) Example\n\nPermission marker shared by both packages.'
    first_license.write_text(license_text, encoding='utf-8')
    second_license.write_text(license_text, encoding='utf-8')

    RunCollector(tmp_path, '--root', str(first_root), '--root', str(second_root))
    document = (tmp_path / 'licenses.md').read_text(encoding='utf-8')

    tokens = MarkdownIt('commonmark').parse(document)
    assert not any(token.type == 'heading_open' and token.tag == 'h1' for token in tokens)
    assert document.startswith('## test\n\n### Additional License Documents\n')
    assert document.count('### Additional License Documents') == 1
    assert f'#### {first_root.as_posix()}: component-one/LICENSE' in document
    assert f'#### {second_root.as_posix()}: nested/component-two/LICENSE.txt' in document
    assert document.count('##### LICENSE') == 1
    assert document.count('Permission marker shared by both packages.') == 1
    assert f'already included for `{first_root.as_posix()}: component-one/LICENSE`' in document


def test_collector_keeps_explicit_copyright_in_mit_fallback(tmp_path: Path) -> None:
    python_root = tmp_path / 'python'
    CreatePythonMetadata(
        python_root,
        name='example-package',
        version='1.0.0',
        extra='Copyright: Copyright (c) 2024 Example Author\n',
    )

    RunCollector(tmp_path, '--python-root', str(python_root))
    document = (tmp_path / 'licenses.md').read_text(encoding='utf-8')

    assert 'Copyright (c) 2024 Example Author' in document
    assert 'Permission is hereby granted, free of charge' in document


def test_collector_rejects_ambiguous_mit_fallback(tmp_path: Path) -> None:
    python_root = tmp_path / 'python'
    CreatePythonMetadata(python_root, name='ambiguous-package', version='1.0.0')

    result = RunCollector(tmp_path, '--python-root', str(python_root), check=False)

    assert result.returncode != 0
    assert 'No full MIT license text or explicit copyright notice is available' in result.stderr


def test_collector_requires_exact_python_license_override_match(tmp_path: Path) -> None:
    python_root = tmp_path / 'python'
    CreatePythonMetadata(python_root, name='grapheme', version='0.6.0')
    override_path = tmp_path / 'grapheme-LICENSE'
    override_path.write_text('Copyright (c) Verified holders\n\nVerified MIT license text.', encoding='utf-8')

    RunCollector(
        tmp_path,
        '--python-root',
        str(python_root),
        '--python-license-override',
        f'grapheme==0.6.0={override_path}',
    )
    document = (tmp_path / 'licenses.md').read_text(encoding='utf-8')
    assert 'Verified MIT license text.' in document

    result = RunCollector(
        tmp_path,
        '--python-root',
        str(python_root),
        '--python-license-override',
        f'grapheme==0.6.1={override_path}',
        check=False,
    )
    assert result.returncode != 0
    assert 'No full MIT license text or explicit copyright notice is available' in result.stderr


def test_collector_repairs_only_known_dpkg_copyright_corruption() -> None:
    collector = runpy.run_path(str(COLLECTOR_PATH))
    repair = cast(
        Callable[[str, str, Path, str | None], str | None],
        collector['repairDpkgCopyrightContent'],
    )
    path = Path('/usr/share/doc/bsdutils/copyright')
    damaged = 'Copyright:\n                     Bartosz Fe�ski <fenio@o2.pl>'

    repaired = repair('bsdutils', '1:2.37.2-4ubuntu3.5', path, damaged)
    assert repaired == 'Copyright:\n                     Bartosz Feński <fenio@o2.pl>'

    jquery_path = Path('/usr/share/doc/libjs-jquery-metadata/copyright')
    jquery_damaged = 'Copyright: John Resig, J�örn Zaefferer'
    jquery_repaired = repair('libjs-jquery-metadata', '12-3', jquery_path, jquery_damaged)
    assert jquery_repaired == 'Copyright: John Resig, Jörn Zaefferer'

    with pytest.raises(RuntimeError, match='Unknown Unicode replacement character'):
        repair('bsdutils', '1:2.37.2-4ubuntu3.6', path, damaged)


def test_collector_identifies_duplicate_python_packages_by_install_root(tmp_path: Path) -> None:
    first_root = tmp_path / 'venv'
    second_root = tmp_path / 'standalone-python'
    for root in [first_root, second_root]:
        CreatePythonMetadata(root, name='shared-package', version='1.0.0')
        license_path = root / 'shared-package-1.0.0.dist-info/LICENSE'
        license_path.write_text('Copyright (c) Shared\n\nShared license.', encoding='utf-8')

    RunCollector(tmp_path, '--python-root', str(first_root), '--python-root', str(second_root))
    document = (tmp_path / 'licenses.md').read_text(encoding='utf-8')

    assert f'#### shared-package 1.0.0 — {first_root.as_posix()}' in document
    assert f'#### shared-package 1.0.0 — {second_root.as_posix()}' in document


def test_license_html_preserves_headings_scrolls_tables_and_uses_http_cache(tmp_path: Path) -> None:
    licenses_path = tmp_path / 'THIRD_PARTY_LICENSES.md'
    licenses_path.write_text(
        '# Licenses\n\n## Example package\n\n'
        '| Name | Version | License |\n| --- | --- | --- |\n| Example | 1.0 | MIT |\n',
        encoding='utf-8',
    )
    VersionRouter.THIRD_PARTY_LICENSES_PATH = licenses_path
    VersionRouter._license_document_cache = None
    app = FastAPI()
    app.include_router(VersionRouter.router)

    async def RequestLicenses() -> tuple[str, str, int, str]:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get(
                '/api/version/third-party-licenses',
                headers={'Accept-Encoding': 'gzip'},
            )
            etag = response.headers['etag']
            cached_response = await client.get(
                '/api/version/third-party-licenses',
                headers={'If-None-Match': etag, 'Accept-Encoding': 'gzip'},
            )
            return response.text, response.headers['content-encoding'], cached_response.status_code, etag

    document, content_encoding, cached_status, etag = asyncio.run(RequestLicenses())

    assert (
        '<summary class="license-section__summary license-section__summary--level-2">'
        '<h2 class="license-section__heading license-section__heading--level-2">'
    ) in document
    assert '<span class="license-section__title">Example package</span>' in document
    assert '<span class="license-section__state" aria-hidden="true">' in document
    assert '</span></span></h2></summary>' in document
    assert '<div class="table-scroll" role="region" tabindex="0"' in document
    assert content_encoding == 'gzip'
    assert cached_status == 304
    assert etag.startswith('W/"') and etag.endswith('"')


def test_chromium_license_route_serves_raw_markdown_with_compression_and_cache(tmp_path: Path) -> None:
    chromium_licenses_path = tmp_path / 'CHROMIUM_THIRD_PARTY_LICENSES.md'
    chromium_licenses_path.write_text('# Chromium licenses\n\n## Component\n\nLicense text.\n', encoding='utf-8')
    VersionRouter.CHROMIUM_THIRD_PARTY_LICENSES_PATH = chromium_licenses_path
    VersionRouter._chromium_license_document_cache = None
    app = FastAPI()
    app.include_router(VersionRouter.router)

    async def RequestLicenses() -> tuple[str, str, str, str, int]:
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get(
                '/api/version/chromium-third-party-licenses',
                headers={'Accept-Encoding': 'gzip'},
            )
            etag = response.headers['etag']
            cached_response = await client.get(
                '/api/version/chromium-third-party-licenses',
                headers={'If-None-Match': etag},
            )
            return (
                response.text,
                response.headers['content-type'],
                response.headers['content-encoding'],
                response.headers['content-disposition'],
                cached_response.status_code,
            )

    document, content_type, content_encoding, content_disposition, cached_status = asyncio.run(RequestLicenses())

    assert document == '# Chromium licenses\n\n## Component\n\nLicense text.\n'
    assert content_type == 'text/markdown; charset=utf-8'
    assert content_encoding == 'gzip'
    assert content_disposition == 'inline; filename="CHROMIUM_THIRD_PARTY_LICENSES.md"'
    assert cached_status == 304
