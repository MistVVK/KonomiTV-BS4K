#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = ROOT / 'public/assets/fonts'
LICENSE_DIR = ROOT / 'licenses/fonts'

PACKAGES = (
    ('https://registry.npmjs.org/@fontsource/kosugi/-/kosugi-5.2.5.tgz', '7292df899c500f33169e5d2de02e242a6b77c00036a838305cb1b0f431966343',
     {'package/files/kosugi-japanese-400-normal.woff2': 'Kosugi-Regular.woff2'}, 'Kosugi-LICENSE.txt'),
    ('https://registry.npmjs.org/@fontsource/kosugi-maru/-/kosugi-maru-5.2.5.tgz', '802689a7aa9927c515c5910aae019fd571241e48277f03a9b9795a6b60bdc11d',
     {'package/files/kosugi-maru-japanese-400-normal.woff2': 'KosugiMaru-Regular.woff2'}, 'KosugiMaru-LICENSE.txt'),
    ('https://registry.npmjs.org/@fontsource/open-sans/-/open-sans-5.2.7.tgz', '511ee4e6104ebcb0753462c8473dce7734e2d20438bd5172c829acd66b886061',
     {'package/files/open-sans-latin-500-normal.woff2': 'OpenSans-Medium.woff2', 'package/files/open-sans-latin-700-normal.woff2': 'OpenSans-Bold.woff2'}, 'OpenSans-LICENSE.txt'),
    ('https://registry.npmjs.org/@fontsource/noto-sans-jp/-/noto-sans-jp-5.2.9.tgz', 'e4d4ba8825aebe6293e19400a07d45b8f111ff31532c8dfbdd77b986c0123652',
     {'package/files/noto-sans-jp-japanese-500-normal.woff2': 'NotoSansJP-Medium.woff2', 'package/files/noto-sans-jp-japanese-700-normal.woff2': 'NotoSansJP-Bold.woff2'}, 'NotoSansJP-LICENSE.txt'),
    ('https://registry.npmjs.org/yakuhanjp/-/yakuhanjp-4.1.1.tgz', 'e0fbbc7c278098fb08f55d1123a2a59b04051d4f754c62e8d70d55f711a80bd7',
     {'package/dist/fonts/YakuHanJPs/YakuHanJPs-Medium.woff2': 'YakuHanJPs-Medium.woff2', 'package/dist/fonts/YakuHanJPs/YakuHanJPs-Bold.woff2': 'YakuHanJPs-Bold.woff2'}, None),
    ('https://registry.npmjs.org/@mdi/font/-/font-7.4.47.tgz', '66131558352dc8df724feac30f3b7bf7ee7311a79e4f8436f3637c0993edb773',
     {'package/fonts/materialdesignicons-webfont.woff2': 'MaterialDesignIcons.woff2'}, 'MaterialDesignIcons-LICENSE.txt'),
)

TWEMOJI_URL = 'https://github.com/mozilla/twemoji-colr/releases/download/v0.7.0/Twemoji.Mozilla.ttf'
TWEMOJI_SHA256 = '6d90152ee0d29e82fe2a87793af5aa4b7ad13e6538360889e141e81ed299ee8e'


def download(url: str, expected: str) -> bytes:
    data = urllib.request.urlopen(url).read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(f'SHA-256 mismatch for {url}: {actual}')
    return data


def main() -> None:
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    for url, digest, files, licenseName in PACKAGES:
        archive = tarfile.open(fileobj=io.BytesIO(download(url, digest)), mode='r:gz')
        for source, destination in files.items():
            (FONT_DIR / destination).write_bytes(archive.extractfile(source).read())
        if licenseName is not None:
            licenseText = archive.extractfile('package/LICENSE').read().decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
            (LICENSE_DIR / licenseName).write_text(licenseText, encoding='utf-8', newline='\n')

    if not TWEMOJI_SHA256:
        raise RuntimeError('Set TWEMOJI_SHA256 to the verified official release asset hash.')
    with tempfile.TemporaryDirectory() as tempDirectory:
        temp = Path(tempDirectory)
        source = temp / 'Twemoji.Mozilla.ttf'
        source.write_bytes(download(TWEMOJI_URL, TWEMOJI_SHA256))
        environment = temp / 'fonttools'
        subprocess.run([sys.executable, '-m', 'venv', environment], check=True)
        subprocess.run([environment / 'bin/pip', 'install', '--disable-pip-version-check', 'fonttools[woff]==4.59.1'], check=True)
        subprocess.run([environment / 'bin/fonttools', 'ttLib.woff2', 'compress', source, '-o', FONT_DIR / 'Twemoji.woff2'], check=True)


if __name__ == '__main__':
    main()
