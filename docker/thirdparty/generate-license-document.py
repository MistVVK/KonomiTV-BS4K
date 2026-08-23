#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import io
import os
import tarfile
import urllib.request
from dataclasses import dataclass
from functools import cache
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = Path(__file__).resolve().with_name('manifest.env')
LICENSE_MANIFEST_PATH = Path(__file__).resolve().with_name('license-manifest.env')
PATCHES_ROOT = Path(__file__).resolve().with_name('patches')


def localFileSha256(path: Path) -> str:
    """リポジトリ内ファイルの SHA-256 を実測する。

    Args:
        path: ハッシュを算出するリポジトリ内ファイルのパス。

    Returns:
        ファイル内容の SHA-256 (小文字 hex)。
    """

    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class LicenseSource:
    name: str
    version: str
    source_url: str
    fixed_value: str
    files: tuple[tuple[str, str], ...]


def loadManifest() -> dict[str, str]:
    """固定 manifest を読み込む。"""

    manifest: dict[str, str] = {}
    for path in (MANIFEST_PATH, LICENSE_MANIFEST_PATH):
        for line in path.read_text(encoding='utf-8').splitlines():
            if line == '' or line.startswith('#'):
                continue
            key, value = line.split('=', maxsplit=1)
            manifest[key] = value
    return manifest


@cache
def download(url: str) -> bytes:
    """URL からライセンスまたは固定 archive を取得する。"""

    cache_root_text = os.environ.get('KONOMITV_THIRDPARTY_LICENSE_CACHE')
    cache_path = None
    if cache_root_text is not None:
        cache_root = Path(cache_root_text)
        cache_path = cache_root / hashlib.sha256(url.encode('utf-8')).hexdigest()
        if cache_path.is_file():
            return cache_path.read_bytes()
    request = urllib.request.Request(url, headers={'User-Agent': 'KonomiTV-BS4K-thirdparty-license-generator'})
    with urllib.request.urlopen(request) as response:
        content = response.read()
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(content)
    return content


def decodeLicense(content: bytes) -> str:
    """UTF-8 と CP932 のライセンスファイルを改変せず文字列化する。"""

    for encoding in ('utf-8-sig', 'cp932'):
        try:
            decoded = content.decode(encoding).replace('\r\n', '\n').rstrip()
            return '\n'.join(line.rstrip() for line in decoded.splitlines())
        except UnicodeDecodeError:
            continue
    raise ValueError('Unsupported license file encoding.')


def readLicense(location: str) -> str:
    """固定 URL またはローカルファイルのライセンス本文を読み込む。

    URL は commit 固定の GitHub raw のみを使う。git の content addressing が
    完全性を担保するため、個別の checksum 照合は行わない
    (上流 commit を bump するたびに checksum も更新する運用はビルド停止の温床になる)。
    """

    content = download(location) if location.startswith('https://') else Path(location).read_bytes()
    return decodeLicense(content)


def readHeaderLicense(location: str) -> str:
    """固定 URL のヘッダーファイルから、ファイル先頭のライセンスコメントだけを取得する。"""

    decoded = decodeLicense(download(location))
    if not decoded.startswith('/*') or '*/' not in decoded:
        raise ValueError(f'License header not found: {location}')
    return decoded[2:decoded.index('*/')].replace(' * ', '').replace(' *', '').strip()


def extractTarLicense(url: str, sha256: str, member_name: str) -> str:
    """checksum を検証した tar.gz からライセンス本文を抽出する。"""

    archive = download(url)
    if hashlib.sha256(archive).hexdigest() != sha256:
        raise ValueError(f'Checksum mismatch: {url}')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:*') as tar:
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
        LicenseSource('KonomiTV upstream', manifest['KONOMITV_UPSTREAM_VERSION'], manifest['KONOMITV_UPSTREAM_REPOSITORY'], manifest['KONOMITV_UPSTREAM_COMMIT'], (
            ('License.txt', f'{github_raw}/tsukumijima/KonomiTV/{manifest["KONOMITV_UPSTREAM_COMMIT"]}/License.txt'),
        )),
        LicenseSource('FFmpeg', manifest['FFMPEG8_VERSION'], manifest['FFMPEG8_REPOSITORY'], manifest['FFMPEG8_COMMIT'], (
            ('LICENSE.md', f'{github_raw}/FFmpeg/FFmpeg/{manifest["FFMPEG8_COMMIT"]}/LICENSE.md'),
            ('GNU LGPL version 2.1', f'{github_raw}/FFmpeg/FFmpeg/{manifest["FFMPEG8_COMMIT"]}/COPYING.LGPLv2.1'),
            ('GNU GPL version 3', f'{github_raw}/FFmpeg/FFmpeg/{manifest["FFMPEG8_COMMIT"]}/COPYING.GPLv3'),
        )),
        LicenseSource('libaribtlv', manifest['LIBARIBTLV_VERSION'], manifest['LIBARIBTLV_REPOSITORY'], manifest['LIBARIBTLV_COMMIT'], (
            ('LICENSE', f'{github_raw}/makeding/libaribtlv/{manifest["LIBARIBTLV_COMMIT"]}/LICENSE'),
        )),
        LicenseSource('FFmpeg libaribtlv integration patches', manifest['FFMPEG8_VERSION'], manifest['FFMPEG_LIBARIBTLV_REPOSITORY'], manifest['FFMPEG_LIBARIBTLV_COMMIT'], (
            ('LICENSE', f'{github_raw}/makeding/ffmpeg-libaribtlv/{manifest["FFMPEG_LIBARIBTLV_COMMIT"]}/LICENSE'),
        )),
        LicenseSource('AMD Advanced Media Framework headers', manifest['AMF_VERSION'], manifest['AMF_REPOSITORY'], manifest['AMF_COMMIT'], (
            ('LICENSE.txt', f'{github_raw}/GPUOpen-LibrariesAndSDKs/AMF/{manifest["AMF_COMMIT"]}/LICENSE.txt'),
        )),
        LicenseSource('tsreadex', manifest['TSREADEX_COMMIT'][:12], 'https://github.com/MistVVK/tsreadex', manifest['TSREADEX_COMMIT'], (
            ('License.txt', str(REPOSITORY_ROOT / 'thirdparty-src/tsreadex/License.txt')),
        )),
        LicenseSource('psisiarc', manifest['PSISIARC_COMMIT'][:12], manifest['PSISIARC_REPOSITORY'], manifest['PSISIARC_COMMIT'], (
            ('License.txt', f'{github_raw}/xtne6f/psisiarc/{manifest["PSISIARC_COMMIT"]}/License.txt'),
        )),
        LicenseSource('Akebi HTTPS Server', manifest['AKEBI_COMMIT'][:12], manifest['AKEBI_REPOSITORY'], manifest['AKEBI_COMMIT'], (
            ('License.txt', f'{github_raw}/tsukumijima/Akebi/{manifest["AKEBI_COMMIT"]}/License.txt'),
        )),
        LicenseSource('Poetry', manifest['POETRY_VERSION'], 'https://github.com/python-poetry/poetry', '19a2f7bddb9bdf931a229ea0913a84021f3f9b93', (
            ('LICENSE', f'{github_raw}/python-poetry/poetry/19a2f7bddb9bdf931a229ea0913a84021f3f9b93/LICENSE'),
        )),
        LicenseSource('AviSynth+', manifest['AVISYNTHPLUS_TAG'], manifest['AVISYNTHPLUS_REPOSITORY'], manifest['AVISYNTHPLUS_COMMIT'], (
            ('GNU GPL version 2', f'{github_raw}/AviSynth/AviSynthPlus/{manifest["AVISYNTHPLUS_COMMIT"]}/distrib/gpl.txt'),
        )),
        LicenseSource('FFmpegSource2', manifest['FFMS2_VERSION'], manifest['FFMS2_REPOSITORY'], manifest['FFMS2_COMMIT'], (
            ('COPYING', f'{github_raw}/FFMS/ffms2/{manifest["FFMS2_COMMIT"]}/COPYING'),
        )),
        LicenseSource('chapter_exe', manifest['CHAPTER_EXE_COMMIT'][:12], manifest['CHAPTER_EXE_REPOSITORY'], manifest['CHAPTER_EXE_COMMIT'], (
            ('LICENSE', f'{github_raw}/rigaya/chapter_exe/{manifest["CHAPTER_EXE_COMMIT"]}/LICENSE'),
        )),
        LicenseSource('logoframe', manifest['LOGOFRAME_VERSION'], manifest['LOGOFRAME_REPOSITORY'], manifest['LOGOFRAME_COMMIT'], (
            ('LICENSE', f'{github_raw}/tobitti0/logoframe/{manifest["LOGOFRAME_COMMIT"]}/LICENSE'),
        )),
        LicenseSource('join_logo_scp', manifest['JOIN_LOGO_SCP_COMMIT'][:12], manifest['JOIN_LOGO_SCP_REPOSITORY'], manifest['JOIN_LOGO_SCP_COMMIT'], (
            ('LICENSE', f'{github_raw}/tobitti0/join_logo_scp/{manifest["JOIN_LOGO_SCP_COMMIT"]}/LICENSE'),
        )),
        LicenseSource('Intel gmmlib', manifest['INTEL_GMMLIB_VERSION'], 'https://github.com/intel/gmmlib', manifest['INTEL_GMMLIB_COMMIT'], (
            ('LICENSE.md', f'{github_raw}/intel/gmmlib/{manifest["INTEL_GMMLIB_COMMIT"]}/LICENSE.md'),
        )),
        LicenseSource('Intel libva', manifest['INTEL_LIBVA_VERSION'], 'https://github.com/intel/libva', manifest['INTEL_LIBVA_COMMIT'], (
            ('COPYING', f'{github_raw}/intel/libva/{manifest["INTEL_LIBVA_COMMIT"]}/COPYING'),
        )),
        LicenseSource('Intel Media Driver', manifest['INTEL_MEDIA_DRIVER_VERSION'], 'https://github.com/intel/media-driver', manifest['INTEL_MEDIA_DRIVER_COMMIT'], (
            ('LICENSE.md', f'{github_raw}/intel/media-driver/{manifest["INTEL_MEDIA_DRIVER_COMMIT"]}/LICENSE.md'),
        )),
        LicenseSource('Intel Media SDK', manifest['INTEL_MEDIASDK_VERSION'], 'https://github.com/Intel-Media-SDK/MediaSDK', manifest['INTEL_MEDIASDK_COMMIT'], (
            ('LICENSE', f'{github_raw}/Intel-Media-SDK/MediaSDK/{manifest["INTEL_MEDIASDK_COMMIT"]}/LICENSE'),
        )),
        LicenseSource('Intel oneVPL GPU Runtime', manifest['INTEL_ONEVPL_GPU_VERSION'], 'https://github.com/intel/vpl-gpu-rt', manifest['INTEL_ONEVPL_GPU_COMMIT'], (
            ('LICENSE', f'{github_raw}/intel/vpl-gpu-rt/{manifest["INTEL_ONEVPL_GPU_COMMIT"]}/LICENSE'),
        )),
    )

    document = [
        '# Third-Party Software Licenses',
        '',
        '<!-- NONFREE_RUNTIME_WARNING_START -->',
        '> **重要: この Docker イメージは再配布しないでください。**',
        '>',
        '<!-- INTEL_NONFREE_WARNING_START -->',
        '> このビルドプロファイルには、Intel Media Driver の Full Feature 版（`ENABLE_NONFREE_KERNELS=ON`）が含まれます。',
        '<!-- INTEL_NONFREE_WARNING_END -->',
        '>',
        '<!-- AMD_NONFREE_WARNING_START -->',
        '> このビルドプロファイルには、AMD proprietary runtime（`amf-amdgpu-pro`、`libamdenc-amdgpu-pro`、`vulkan-amdgpu-pro` など）が含まれます。',
        '>',
        '> AMD proprietary runtime に同梱される AMD Software End User License Agreement は、Software の配布・公開・表示・サブライセンス・譲渡・移転を禁止し、AMD プロセッサーを組み込んだシステムまたはコンポーネントでのインストールと使用に限定しています。したがって、これらのパッケージを含む完成イメージを再配布または移転せず、EULA が許諾する AMD 環境でのローカル利用に限定してください。AMD の現行 EULA: <https://www.amd.com/en/legal/eula/amd-software-eula.html>',
        '<!-- AMD_NONFREE_WARNING_END -->',
        '>',
        '> また、完成イメージには GPL version 3 or later で提供される FFmpeg など、別の条件が適用されるソフトウェアも含まれます。この注意書きは各ライセンスの条件を変更したり、追加の権利を許諾したりするものではありません。利用者自身で、完成イメージに含まれるすべてのライセンス条件を確認してください。',
        '<!-- NONFREE_RUNTIME_WARNING_END -->',
        '',
        '<!--',
        'KonomiTV-BS4K の Docker image に直接組み込む third-party ソフトウェアの著作権表示とライセンス全文です。',
        'ライセンスの種類にかかわらず、取得元に含まれるライセンス本文を省略せず掲載しています。',
        '',
        'このファイルは `docker/thirdparty/generate-license-document.py` から生成します。手動編集しないでください。',
        'Ubuntu・CUDA・GPU runtime・Python／JavaScript パッケージの推移的依存関係を含め、各配布物の著作権表示とライセンス全文を掲載します。',
        'Chromium の大容量なライセンス全文は、同じイメージ内の専用文書へ分離してこの文書からリンクします。',
        '-->',
        '',
        '## Directly Managed Third-Party Components',
        '',
        '### Bundled Components',
        '',
        '#### Chromium',
        '',
        '- Source package and binaries: <https://packages.linuxmint.com/>',
        '- Upstream source: <https://chromium.googlesource.com/chromium/src/>',
        '- Package: `chromium` from the signed Linux Mint Virginia `upstream` repository',
        '- Licenses include: BSD 3-Clause and the licenses listed by the installed Chromium binary',
        '',
        'The exact Chromium license and every third-party notice reported by the installed binary are generated during the Docker build.',
        '',
        '[Open the complete Chromium license document](/api/version/chromium-third-party-licenses)',
        '',
    ]

    for source in sources:
        document.extend([
            f'#### {source.name} {source.version}'.rstrip(),
            '',
            f'- Source: <{source.source_url}>',
            f'- Fixed revision or artifact: `{source.fixed_value}`',
            '',
        ])
        for label, location in source.files:
            license_text = readLicense(location)
            document.extend([f'##### {label}', '', '```text', license_text, '```', ''])

    # nv-codec-headers の各ヘッダーの先頭 notice を読み、同一 notice を重複掲載しない。
    # 上流が notice を変更しても失敗させず、検出した distinct notice をすべて掲載する。
    nvcodec_header_names = (
        'nvEncodeAPI.h',
        'dynlink_cuda.h',
        'dynlink_cuviddec.h',
        'dynlink_loader.h',
        'dynlink_nvcuvid.h',
    )
    # notice 本文から、その notice を共有するヘッダー名の一覧への写像を組み立てる。
    nvcodec_notice_groups: dict[str, list[str]] = {}
    for header_name in nvcodec_header_names:
        header_url = (
            f'{github_raw}/FFmpeg/nv-codec-headers/{manifest["NVCODEC_HEADERS_COMMIT"]}'
            f'/include/ffnvcodec/{header_name}'
        )
        notice = readHeaderLicense(header_url)
        nvcodec_notice_groups.setdefault(notice, []).append(header_name)
    document.extend([
        f'#### NVIDIA codec API headers {manifest["NVCODEC_HEADERS_VERSION"]}', '',
        f'- Source: <{manifest["NVCODEC_HEADERS_REPOSITORY"]}>',
        f'- Fixed revision or artifact: `{manifest["NVCODEC_HEADERS_COMMIT"]}`', '',
    ])
    for notice, header_names in nvcodec_notice_groups.items():
        document.extend([f'##### {" / ".join(header_names)} license notice', '', '```text', notice, '```', ''])

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
            f'#### {name} {version}', '',
            f'- Source: <{source_url}>',
            f'- SHA-256: `{sha256}`', '',
        ])
        for label, member_name in license_files:
            license_text = extractTarLicense(source_url, sha256, member_name)
            document.extend([f'##### {label}', '', '```text', license_text, '```', ''])

    python_license = extractTarLicense(
        manifest['PYTHON_URL'], manifest['PYTHON_SHA256'], 'python/lib/python3.11/LICENSE.txt',
    )
    document.extend([
        f'#### Python Standalone {manifest["PYTHON_VERSION"]}', '',
        f'- Source: <{manifest["PYTHON_URL"]}>',
        f'- SHA-256: `{manifest["PYTHON_SHA256"]}`', '',
        '##### LICENSE.txt', '', '```text', python_license, '```', '',
    ])

    go_license = extractTarLicense(manifest['GO_URL'], manifest['GO_SHA256'], 'go/LICENSE')
    document.extend([
        f'#### Go {manifest["GO_VERSION"]}', '',
        f'- Source: <{manifest["GO_URL"]}>',
        f'- SHA-256: `{manifest["GO_SHA256"]}`', '',
        '##### LICENSE', '', '```text', go_license, '```', '',
        '### Corresponding Source and Local Modifications',
        '',
        '#### CM analysis runtime', '',
        '- This runtime does not include Amatsukaze itself.',
        f'- FFmpeg source: <{manifest["FFMPEG8_REPOSITORY"]}> (`{manifest["FFMPEG8_COMMIT"]}`; LGPL-only build)',
        f'- AviSynth+ source: <{manifest["AVISYNTHPLUS_REPOSITORY"]}> (`{manifest["AVISYNTHPLUS_COMMIT"]}`)',
        f'- FFmpegSource2 source: <{manifest["FFMS2_REPOSITORY"]}> (`{manifest["FFMS2_COMMIT"]}`; AviSynth-only build)',
        f'- chapter_exe source: <{manifest["CHAPTER_EXE_REPOSITORY"]}> (`{manifest["CHAPTER_EXE_COMMIT"]}`)',
        f'- logoframe source: <{manifest["LOGOFRAME_REPOSITORY"]}> (`{manifest["LOGOFRAME_COMMIT"]}`)',
        f'- join_logo_scp source: <{manifest["JOIN_LOGO_SCP_REPOSITORY"]}> (`{manifest["JOIN_LOGO_SCP_COMMIT"]}`)',
        '- Reproducible build procedure: `docker/thirdparty/build-cm-analysis.sh`',
        '- Local patches:',
        # patch の SHA-256 は固定値として manifest に持たず、リポジトリ内の実ファイルから都度算出する。
        # 事前固定すると patch 更新のたびに manifest 更新も必要になり、忘れた場合にビルドが止まる。
        f'  - `chapter-exe-initialize-avisynth.patch` (`{localFileSha256(PATCHES_ROOT / "chapter-exe-initialize-avisynth.patch")}`)',
        f'  - `ffms2-hardware-decoding.patch` (`{localFileSha256(PATCHES_ROOT / "ffms2-hardware-decoding.patch")}`)',
        f'  - `logoframe-error-lifetime.patch` (`{localFileSha256(PATCHES_ROOT / "logoframe-error-lifetime.patch")}`)',
        f'  - `logoframe-parallel-scan.patch` (`{localFileSha256(PATCHES_ROOT / "logoframe-parallel-scan.patch")}`)',
        f'  - `logoframe-native-luma.patch` (`{localFileSha256(PATCHES_ROOT / "logoframe-native-luma.patch")}`)',
        f'  - `logoframe-high-bit-rgb-fallback.patch` (`{localFileSha256(PATCHES_ROOT / "logoframe-high-bit-rgb-fallback.patch")}`)',
        '',
        'The fixed upstream revisions, complete local patches, dependency revisions, and build commands above are the corresponding source recipe for the redistributed native artifacts.',
        '',
        '#### FFmpeg 8 and AMF', '',
        f'- FFmpeg source: <{manifest["FFMPEG8_REPOSITORY"]}>',
        f'- FFmpeg fixed commit: `{manifest["FFMPEG8_COMMIT"]}`',
        f'- libaribtlv source: <{manifest["LIBARIBTLV_REPOSITORY"]}> (`{manifest["LIBARIBTLV_COMMIT"]}`)',
        f'- FFmpeg libaribtlv patch source: <{manifest["FFMPEG_LIBARIBTLV_REPOSITORY"]}> (`{manifest["FFMPEG_LIBARIBTLV_COMMIT"]}`)',
        f'- AMF source: <{manifest["AMF_REPOSITORY"]}>',
        f'- AMF fixed commit: `{manifest["AMF_COMMIT"]}`',
        '- Reproducible build procedure: `docker/thirdparty/build-ffmpeg8.sh`',
        '- Local patches:',
        f'  - `libaribtlv-0.2.0-konomitv-subtitle-mfu.patch` (`{localFileSha256(PATCHES_ROOT / "libaribtlv-0.2.0-konomitv-subtitle-mfu.patch")}`)',
        f'  - `ffmpeg-8.1.2-libaribtlv-timed-id3.patch` (`{localFileSha256(PATCHES_ROOT / "ffmpeg-8.1.2-libaribtlv-timed-id3.patch")}`)',
        f'  - `ffmpeg-8.1.2-libaribtlv-context-id-metadata.patch` (`{localFileSha256(PATCHES_ROOT / "ffmpeg-8.1.2-libaribtlv-context-id-metadata.patch")}`)',
        f'  - `ffmpeg-8.1.2-vaapi-mesa-hevc-alignment.patch` (`{localFileSha256(PATCHES_ROOT / "ffmpeg-8.1.2-vaapi-mesa-hevc-alignment.patch")}`)',
        f'  - `amf-1.4.36-display-capture-c.patch` (`{localFileSha256(PATCHES_ROOT / "amf-1.4.36-display-capture-c.patch")}`)',
        '',
        '#### Intel media stack', '',
        f'- libva source: <https://github.com/intel/libva> (`{manifest["INTEL_LIBVA_COMMIT"]}`)',
        f'- Intel Media Driver source: <https://github.com/intel/media-driver> (`{manifest["INTEL_MEDIA_DRIVER_COMMIT"]}`)',
        f'- oneVPL GPU Runtime source: <https://github.com/intel/vpl-gpu-rt> (`{manifest["INTEL_ONEVPL_GPU_COMMIT"]}`)',
        '- Reproducible build procedure: `docker/thirdparty/build-intel-media-stack.sh`',
        '- Local patches:',
        f'  - `intel-libva-standalone.patch` (`{localFileSha256(PATCHES_ROOT / "intel-libva-standalone.patch")}`)',
        f'  - `intel-media-driver-vpp-deinterlace-crash-fix.patch` (`{localFileSha256(PATCHES_ROOT / "intel-media-driver-vpp-deinterlace-crash-fix.patch")}`)',
        f'  - `intel-onevpl-gpu-rt-vpp-deinterlace-hang-fix.patch` (`{localFileSha256(PATCHES_ROOT / "intel-onevpl-gpu-rt-vpp-deinterlace-hang-fix.patch")}`)',
        '',
    ])

    args.output.write_text('\n'.join(document), encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
