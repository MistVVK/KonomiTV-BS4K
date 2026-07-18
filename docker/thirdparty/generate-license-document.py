#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import io
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = Path(__file__).resolve().with_name('manifest.env')


@dataclass(frozen=True)
class LicenseSource:
    name: str
    version: str
    source_url: str
    fixed_value: str
    files: tuple[tuple[str, str, str], ...]


def loadManifest() -> dict[str, str]:
    """固定 manifest を読み込む。"""

    manifest: dict[str, str] = {}
    for line in MANIFEST_PATH.read_text(encoding='utf-8').splitlines():
        if line == '' or line.startswith('#'):
            continue
        key, value = line.split('=', maxsplit=1)
        manifest[key] = value
    return manifest


def download(url: str) -> bytes:
    """URL からライセンスまたは固定 archive を取得する。"""

    request = urllib.request.Request(url, headers={'User-Agent': 'KonomiTV-thirdparty-license-generator'})
    with urllib.request.urlopen(request) as response:
        return response.read()


def decodeLicense(content: bytes) -> str:
    """UTF-8 と CP932 のライセンスファイルを改変せず文字列化する。"""

    for encoding in ('utf-8-sig', 'cp932'):
        try:
            decoded = content.decode(encoding).replace('\r\n', '\n').rstrip()
            return '\n'.join(line.rstrip() for line in decoded.splitlines())
        except UnicodeDecodeError:
            continue
    raise ValueError('Unsupported license file encoding.')


def readVerifiedLicense(location: str, sha256: str) -> str:
    """固定 URL またはローカルファイルのライセンス本文を checksum 検証して読み込む。"""

    content = download(location) if location.startswith('https://') else Path(location).read_bytes()
    if hashlib.sha256(content).hexdigest() != sha256:
        raise ValueError(f'License checksum mismatch: {location}')
    return decodeLicense(content)


def extractTarLicense(url: str, sha256: str, member_name: str) -> str:
    """checksum を検証した tar.gz からライセンス本文を抽出する。"""

    archive = download(url)
    if hashlib.sha256(archive).hexdigest() != sha256:
        raise ValueError(f'Checksum mismatch: {url}')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        member = tar.extractfile(member_name)
        if member is None:
            raise ValueError(f'License not found in archive: {member_name}')
        return decodeLicense(member.read())


def main() -> None:
    """直接同梱する thirdparty のライセンス全文を Markdown にまとめる。"""

    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=REPOSITORY_ROOT / 'THIRD_PARTY_LICENSES.md')
    args = parser.parse_args()

    manifest = loadManifest()
    github_raw = 'https://raw.githubusercontent.com'
    sources = (
        LicenseSource('FFmpeg', manifest['FFMPEG_VERSION'], manifest['FFMPEG_REPOSITORY'], manifest['FFMPEG_COMMIT'], (
            ('LICENSE.md', f'{github_raw}/FFmpeg/FFmpeg/{manifest["FFMPEG_COMMIT"]}/LICENSE.md', manifest['FFMPEG_LICENSE_SHA256']),
            ('GNU GPL version 3', f'{github_raw}/FFmpeg/FFmpeg/{manifest["FFMPEG_COMMIT"]}/COPYING.GPLv3', manifest['FFMPEG_GPLV3_SHA256']),
        )),
        LicenseSource('QSVEncC', manifest['QSVENCC_VERSION'], manifest['QSVENCC_REPOSITORY'], manifest['QSVENCC_COMMIT'], (
            ('license.txt', f'{github_raw}/rigaya/QSVEnc/{manifest["QSVENCC_COMMIT"]}/license.txt', manifest['QSVENCC_LICENSE_SHA256']),
        )),
        LicenseSource('NVEncC', manifest['NVENCC_VERSION'], manifest['NVENCC_REPOSITORY'], manifest['NVENCC_COMMIT'], (
            ('NVEnc_license.txt', f'{github_raw}/rigaya/NVEnc/{manifest["NVENCC_COMMIT"]}/NVEnc_license.txt', manifest['NVENCC_LICENSE_SHA256']),
        )),
        LicenseSource('VCEEncC', manifest['VCEENCC_VERSION'], manifest['VCEENCC_REPOSITORY'], manifest['VCEENCC_COMMIT'], (
            ('VCEEnc_license.txt', f'{github_raw}/rigaya/VCEEnc/{manifest["VCEENCC_COMMIT"]}/VCEEnc_license.txt', manifest['VCEENCC_LICENSE_SHA256']),
        )),
        LicenseSource('tsreadex', manifest['TSREADEX_COMMIT'][:12], 'https://github.com/MistVVK/tsreadex', manifest['TSREADEX_COMMIT'], (
            ('License.txt', str(REPOSITORY_ROOT / 'thirdparty-src/tsreadex/License.txt'), manifest['TSREADEX_LICENSE_SHA256']),
        )),
        LicenseSource('psisiarc', manifest['PSISIARC_COMMIT'][:12], manifest['PSISIARC_REPOSITORY'], manifest['PSISIARC_COMMIT'], (
            ('License.txt', f'{github_raw}/xtne6f/psisiarc/{manifest["PSISIARC_COMMIT"]}/License.txt', manifest['PSISIARC_LICENSE_SHA256']),
        )),
        LicenseSource('psisimux', manifest['PSISIMUX_COMMIT'][:12], manifest['PSISIMUX_REPOSITORY'], manifest['PSISIMUX_COMMIT'], (
            ('License.txt', f'{github_raw}/xtne6f/psisimux/{manifest["PSISIMUX_COMMIT"]}/License.txt', manifest['PSISIMUX_LICENSE_SHA256']),
        )),
        LicenseSource('Akebi HTTPS Server', manifest['AKEBI_COMMIT'][:12], manifest['AKEBI_REPOSITORY'], manifest['AKEBI_COMMIT'], (
            ('License.txt', f'{github_raw}/tsukumijima/Akebi/{manifest["AKEBI_COMMIT"]}/License.txt', manifest['AKEBI_LICENSE_SHA256']),
        )),
        LicenseSource('Poetry', manifest['POETRY_VERSION'], 'https://github.com/python-poetry/poetry', '19a2f7bddb9bdf931a229ea0913a84021f3f9b93', (
            ('LICENSE', f'{github_raw}/python-poetry/poetry/19a2f7bddb9bdf931a229ea0913a84021f3f9b93/LICENSE', manifest['POETRY_LICENSE_SHA256']),
        )),
        LicenseSource('Intel gmmlib', manifest['INTEL_GMMLIB_VERSION'], 'https://github.com/intel/gmmlib', manifest['INTEL_GMMLIB_COMMIT'], (
            ('LICENSE.md', f'{github_raw}/intel/gmmlib/{manifest["INTEL_GMMLIB_COMMIT"]}/LICENSE.md', manifest['INTEL_GMMLIB_LICENSE_SHA256']),
        )),
        LicenseSource('Intel libva', manifest['INTEL_LIBVA_VERSION'], 'https://github.com/intel/libva', manifest['INTEL_LIBVA_COMMIT'], (
            ('COPYING', f'{github_raw}/intel/libva/{manifest["INTEL_LIBVA_COMMIT"]}/COPYING', manifest['INTEL_LIBVA_LICENSE_SHA256']),
        )),
        LicenseSource('Intel Media Driver', manifest['INTEL_MEDIA_DRIVER_VERSION'], 'https://github.com/intel/media-driver', manifest['INTEL_MEDIA_DRIVER_COMMIT'], (
            ('LICENSE.md', f'{github_raw}/intel/media-driver/{manifest["INTEL_MEDIA_DRIVER_COMMIT"]}/LICENSE.md', manifest['INTEL_MEDIA_DRIVER_LICENSE_SHA256']),
        )),
        LicenseSource('Intel Media SDK', manifest['INTEL_MEDIASDK_VERSION'], 'https://github.com/Intel-Media-SDK/MediaSDK', manifest['INTEL_MEDIASDK_COMMIT'], (
            ('LICENSE', f'{github_raw}/Intel-Media-SDK/MediaSDK/{manifest["INTEL_MEDIASDK_COMMIT"]}/LICENSE', manifest['INTEL_MEDIASDK_LICENSE_SHA256']),
        )),
        LicenseSource('Intel oneVPL GPU Runtime', manifest['INTEL_ONEVPL_GPU_VERSION'], 'https://github.com/intel/vpl-gpu-rt', manifest['INTEL_ONEVPL_GPU_COMMIT'], (
            ('LICENSE', f'{github_raw}/intel/vpl-gpu-rt/{manifest["INTEL_ONEVPL_GPU_COMMIT"]}/LICENSE', manifest['INTEL_ONEVPL_GPU_LICENSE_SHA256']),
        )),
    )

    document = [
        '# Third-Party Software Licenses',
        '',
        'KonomiTV の Docker image に直接組み込む third-party ソフトウェアの著作権表示とライセンス全文です。',
        'ライセンスの種類にかかわらず、取得元に含まれるライセンス本文を省略せず掲載しています。',
        '',
        '> このファイルは `docker/thirdparty/generate-license-document.py` から生成します。手動編集しないでください。',
        '> Ubuntu・CUDA・GPU runtime・Python／JavaScript パッケージの推移的依存関係については、各配布物と Docker image 内の `/usr/share/doc` および package metadata に含まれるライセンスも適用されます。',
        '',
    ]

    for source in sources:
        document.extend([
            f'## {source.name} {source.version}',
            '',
            f'- Source: {source.source_url}',
            f'- Fixed revision or artifact: `{source.fixed_value}`',
            '',
        ])
        for label, location, sha256 in source.files:
            license_text = readVerifiedLicense(location, sha256)
            document.extend([f'### {label}', '', '```text', license_text, '```', ''])

    archive_licenses = (
        ('x264', manifest['X264_VERSION'], manifest['X264_SOURCE_URL'], manifest['X264_SOURCE_SHA256'], (
            ('COPYING', 'x264-0.163.3060+git5db6aa6/COPYING'),
        )),
        ('x265', manifest['X265_VERSION'], manifest['X265_SOURCE_URL'], manifest['X265_SOURCE_SHA256'], (
            ('COPYING', 'x265_3.5/COPYING'),
            ('dynamicHDR10/LICENSE.txt', 'x265_3.5/source/dynamicHDR10/LICENSE.txt'),
            ('dynamicHDR10/json11/LICENSE.txt', 'x265_3.5/source/dynamicHDR10/json11/LICENSE.txt'),
        )),
        ('Opus', manifest['OPUS_VERSION'], manifest['OPUS_SOURCE_URL'], manifest['OPUS_SOURCE_SHA256'], (
            ('COPYING', 'opus-1.3.1/COPYING'),
        )),
    )
    for name, version, source_url, sha256, license_files in archive_licenses:
        document.extend([
            f'## {name} {version}', '',
            f'- Source: {source_url}',
            f'- SHA-256: `{sha256}`', '',
        ])
        for label, member_name in license_files:
            license_text = extractTarLicense(source_url, sha256, member_name)
            document.extend([f'### {label}', '', '```text', license_text, '```', ''])

    python_license = extractTarLicense(
        manifest['PYTHON_URL'], manifest['PYTHON_SHA256'], 'python/lib/python3.11/LICENSE.txt',
    )
    document.extend([
        f'## Python Standalone {manifest["PYTHON_VERSION"]}', '',
        f'- Source: {manifest["PYTHON_URL"]}',
        f'- SHA-256: `{manifest["PYTHON_SHA256"]}`', '',
        '### LICENSE.txt', '', '```text', python_license, '```', '',
    ])

    go_license = extractTarLicense(manifest['GO_URL'], manifest['GO_SHA256'], 'go/LICENSE')
    document.extend([
        f'## Go {manifest["GO_VERSION"]}', '',
        f'- Source: {manifest["GO_URL"]}',
        f'- SHA-256: `{manifest["GO_SHA256"]}`', '',
        '### LICENSE', '', '```text', go_license, '```', '',
    ])

    args.output.write_text('\n'.join(document), encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
