import os
import re
import stat
import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPOSITORY_ROOT / 'docker/ts-codec-bridge/manifest.env'
BUILD_SCRIPT_PATH = REPOSITORY_ROOT / 'docker/ts-codec-bridge/build.sh'
DOCKERFILE_PATH = REPOSITORY_ROOT / 'Dockerfile'
EXPECTED_UBUNTU_IMAGE_SHA256 = (
    '0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982'
)
EXPECTED_DOCKERFILE_FRONTEND_SHA256 = (
    'a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e'
)
EXPECTED_MANIFEST_KEYS = (
    'KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_OWNER',
    'KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_NAME',
    'KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT',
    'KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_ARCHIVE_SHA256',
    'KONOMITV_BS4K_TSCODECBRIDGE_CLI_VERSION',
    'KONOMITV_BS4K_TSCODECBRIDGE_TS_MAPPING_VERSION',
    'KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_CODENAME',
    'KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_IMAGE_SHA256',
    'KONOMITV_BS4K_TSCODECBRIDGE_CA_CERTIFICATES_PACKAGE_VERSION',
    'KONOMITV_BS4K_TSCODECBRIDGE_CURL_PACKAGE_VERSION',
    'KONOMITV_BS4K_TSCODECBRIDGE_MAKE_PACKAGE_VERSION',
    'KONOMITV_BS4K_TSCODECBRIDGE_TAR_PACKAGE_VERSION',
    'KONOMITV_BS4K_TSCODECBRIDGE_NALA_PACKAGE_VERSION',
    'KONOMITV_BS4K_TSCODECBRIDGE_SBCL_PACKAGE_VERSION',
    'KONOMITV_BS4K_TSCODECBRIDGE_CL_SWANK_PACKAGE_VERSION',
    'KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_VERSION',
    'KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_SHA256',
    'KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_VERSION',
    'KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_SHA256',
    'KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_COMMIT',
    'KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_SOURCE_SHA256',
    'KONOMITV_BS4K_TSCODECBRIDGE_MALLET_VERSION',
    'KONOMITV_BS4K_TSCODECBRIDGE_MALLET_ARCHIVE_SHA256',
    'KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_VERSION',
    'KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_BINARY_SHA256',
    'KONOMITV_BS4K_TSCODECBRIDGE_FFPROBE_BINARY_SHA256',
)


def ParseManifest(path: Path) -> dict[str, str]:
    """
    shell 展開を行わず、固定 schema の manifest.env を読み込む。

    Args:
        path (Path): 読み込む manifest のパス。

    Returns:
        dict[str, str]: key と安全な単一 token 値の対応。
    """

    values: dict[str, str] = {}
    for line in path.read_text(encoding = 'utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Z0-9_]+)=([A-Za-z0-9._:+~-]+)', line)
        assert match is not None, f'Unsafe manifest line: {line}'
        key, value = match.groups()
        assert key not in values, f'Duplicate manifest key: {key}'
        values[key] = value
    return values


def RunManifestValidation(manifest_path: Path) -> subprocess.CompletedProcess[str]:
    """
    build script の非ネットワーク manifest gate だけを実行する。

    Args:
        manifest_path (Path): 検証対象 manifest のパス。

    Returns:
        subprocess.CompletedProcess[str]: 終了コードと標準出力・標準エラー。
    """

    environment = dict(os.environ)
    environment['KONOMITV_BS4K_TSCODECBRIDGE_MANIFEST_PATH'] = str(manifest_path)
    environment['KONOMITV_BS4K_TSCODECBRIDGE_EXPECTED_UBUNTU_IMAGE_SHA256'] = (
        EXPECTED_UBUNTU_IMAGE_SHA256
    )
    return subprocess.run(
        ['bash', str(BUILD_SCRIPT_PATH), 'validate-manifest'],
        check = False,
        capture_output = True,
        env = environment,
        text = True,
    )


def test_bridge_manifest_has_one_fixed_fail_closed_schema() -> None:
    """
    完全 commit・archive hash・toolchain・FFmpeg が単一 schema で固定されることを検証する。

    Returns:
        None
    """

    manifest = ParseManifest(MANIFEST_PATH)

    assert tuple(manifest) == EXPECTED_MANIFEST_KEYS
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_OWNER'] == 'MistVVK'
    assert (
        manifest['KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_NAME']
        == 'KonomiTV-BS4K-TSCodecBridge'
    )
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_CLI_VERSION'] == '0.1.0'
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_TS_MAPPING_VERSION'] == '1'
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_CODENAME'] == 'jammy'
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_IMAGE_SHA256'] == (
        EXPECTED_UBUNTU_IMAGE_SHA256
    )
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_CA_CERTIFICATES_PACKAGE_VERSION'] == (
        '20260601~22.04.1'
    )
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_CURL_PACKAGE_VERSION'] == (
        '7.81.0-1ubuntu1.25'
    )
    assert (
        manifest['KONOMITV_BS4K_TSCODECBRIDGE_MAKE_PACKAGE_VERSION'] == '4.3-4.1build1'
    )
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_TAR_PACKAGE_VERSION'] == (
        '1.34+dfsg-1ubuntu0.1.22.04.6'
    )
    assert (
        manifest['KONOMITV_BS4K_TSCODECBRIDGE_NALA_PACKAGE_VERSION']
        == '0.11.1~bpo22.04.1'
    )
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_SBCL_PACKAGE_VERSION'] == '2:2.1.11-1'
    assert (
        manifest['KONOMITV_BS4K_TSCODECBRIDGE_CL_SWANK_PACKAGE_VERSION']
        == '2:2.26.1+dfsg-2'
    )
    assert (
        manifest['KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_VERSION']
        == '2.35-0ubuntu3.14'
    )
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_SHA256'] == (
        '4aa2feae34cb6296e133af5c7429756ab5606549cd16a9d26a0060e010214523'
    )
    assert (
        manifest['KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_VERSION']
        == '1:1.2.11.dfsg-2ubuntu9.2'
    )
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_SHA256'] == (
        '9dc17e51a1be2d9ed63b7b84ef0e4e29c5abe6f1bc62cb03e7181483cce8a2f2'
    )
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_COMMIT'] == (
        '1037296f604c3210ce073a53539d4ae95b0c2f8c'
    )
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_SOURCE_SHA256'] == (
        '4d7eec72c1322fd16dc444eb62a7981111965bfce72bf6161bf69e2a6786fd3e'
    )
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_MALLET_VERSION'] == '0.9.2'
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_MALLET_ARCHIVE_SHA256'] == (
        '95d28cc93bf22dd529ea084073478047edb59e690a1a029b85bfbe5579930808'
    )
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_VERSION'] == 'n8.1.2'
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_BINARY_SHA256'] == (
        '5599c2bc61d987c7336ad4a7bf4b917fa8b260822fbb65c8b069188931339f76'
    )
    assert manifest['KONOMITV_BS4K_TSCODECBRIDGE_FFPROBE_BINARY_SHA256'] == (
        '877ca4ba5b63cabde0378cb39af3be12e449b6079c2d975b37aaab20f758c191'
    )

    source_commit = manifest['KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT']
    source_archive_sha256 = manifest[
        'KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_ARCHIVE_SHA256'
    ]
    assert source_commit == 'PENDING_PUBLIC_COMMIT_SHA' or re.fullmatch(
        r'[0-9a-f]{40}', source_commit
    )
    assert (
        source_archive_sha256 == 'PENDING_PUBLIC_SOURCE_ARCHIVE_SHA256'
        or re.fullmatch(
            r'[0-9a-f]{64}',
            source_archive_sha256,
        )
    )
    assert source_commit.startswith('PENDING_') == source_archive_sha256.startswith(
        'PENDING_'
    )


def test_bridge_manifest_validator_rejects_pending_and_accepts_resolved_values(
    tmp_path: Path,
) -> None:
    """
    PENDING 値では必ず終了コード78となり、完全 SHA の組だけを通すことを検証する。

    Args:
        tmp_path (Path): pytest が提供する一時ディレクトリ。

    Returns:
        None
    """

    manifest = ParseManifest(MANIFEST_PATH)
    current_result = RunManifestValidation(MANIFEST_PATH)
    if manifest['KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT'].startswith('PENDING_'):
        assert current_result.returncode == 78
        assert 'reference is pending' in current_result.stderr
    else:
        assert current_result.returncode == 0, current_result.stderr

    manifest['KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT'] = '1' * 40
    manifest['KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_ARCHIVE_SHA256'] = '2' * 64
    resolved_path = tmp_path / 'resolved.env'
    resolved_path.write_text(
        ''.join(f'{key}={manifest[key]}\n' for key in EXPECTED_MANIFEST_KEYS),
        encoding = 'utf-8',
    )
    resolved_result = RunManifestValidation(resolved_path)
    assert resolved_result.returncode == 0, resolved_result.stderr

    manifest['KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT'] = '0' * 40
    zero_commit_path = tmp_path / 'zero-commit.env'
    zero_commit_path.write_text(
        ''.join(f'{key}={manifest[key]}\n' for key in EXPECTED_MANIFEST_KEYS),
        encoding = 'utf-8',
    )
    zero_commit_result = RunManifestValidation(zero_commit_path)
    assert zero_commit_result.returncode != 0
    assert 'all-zero sentinel' in zero_commit_result.stderr

    manifest['KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT'] = '1' * 40
    manifest['KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_ARCHIVE_SHA256'] = '0' * 64
    zero_archive_path = tmp_path / 'zero-archive.env'
    zero_archive_path.write_text(
        ''.join(f'{key}={manifest[key]}\n' for key in EXPECTED_MANIFEST_KEYS),
        encoding = 'utf-8',
    )
    zero_archive_result = RunManifestValidation(zero_archive_path)
    assert zero_archive_result.returncode != 0
    assert 'all-zero sentinel' in zero_archive_result.stderr

    manifest['KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_ARCHIVE_SHA256'] = '2' * 64
    manifest['KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_IMAGE_SHA256'] = '2' * 64
    wrong_base_path = tmp_path / 'wrong-base.env'
    wrong_base_path.write_text(
        ''.join(f'{key}={manifest[key]}\n' for key in EXPECTED_MANIFEST_KEYS),
        encoding = 'utf-8',
    )
    wrong_base_result = RunManifestValidation(wrong_base_path)
    assert wrong_base_result.returncode != 0
    assert 'does not match the Docker base digest' in wrong_base_result.stderr

    manifest['KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_IMAGE_SHA256'] = (
        EXPECTED_UBUNTU_IMAGE_SHA256
    )
    manifest['KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT'] = '0' * 39
    invalid_path = tmp_path / 'invalid.env'
    invalid_path.write_text(
        ''.join(f'{key}={manifest[key]}\n' for key in EXPECTED_MANIFEST_KEYS),
        encoding = 'utf-8',
    )
    invalid_result = RunManifestValidation(invalid_path)
    assert invalid_result.returncode != 0
    assert 'exactly 40 characters' in invalid_result.stderr

    manifest['KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT'] = '1' * 40
    manifest['KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_SHA256'] = 'g' * 64
    invalid_package_hash_path = tmp_path / 'invalid-package-hash.env'
    invalid_package_hash_path.write_text(
        ''.join(f'{key}={manifest[key]}\n' for key in EXPECTED_MANIFEST_KEYS),
        encoding = 'utf-8',
    )
    invalid_package_hash_result = RunManifestValidation(invalid_package_hash_path)
    assert invalid_package_hash_result.returncode != 0
    assert 'lowercase hexadecimal' in invalid_package_hash_result.stderr


def test_bridge_build_script_uses_verified_archives_and_records_runtime_provenance() -> (
    None
):
    """
    build script が branch・latest・Git clone を使わず、全 provenance を記録することを検証する。

    Returns:
        None
    """

    script = BUILD_SCRIPT_PATH.read_text(encoding = 'utf-8')
    syntax_result = subprocess.run(
        ['bash', '-n', str(BUILD_SCRIPT_PATH)],
        check = False,
        capture_output = True,
        text = True,
    )

    assert syntax_result.returncode == 0, syntax_result.stderr
    build_script_stat = BUILD_SCRIPT_PATH.stat()
    build_script_mode = stat.S_IMODE(build_script_stat.st_mode)
    # Git は executable bit だけを配布契約として保持し、group-write は共有
    # worktree の umask / mount policy で変わり得る。固定 0755 ではなく、
    # 実行に必要な regular file・owner executable と world-writable 禁止を検証する。
    assert stat.S_ISREG(build_script_stat.st_mode)
    assert build_script_mode & stat.S_IXUSR
    assert build_script_mode & stat.S_IWOTH == 0
    assert (
        'https://codeload.github.com/${KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_OWNER}/'
        '${KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_NAME}/tar.gz/'
        '${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT}'
    ) in script
    assert 'sha256sum --check --strict -' in script
    assert 'git clone' not in script
    assert '/archive/main' not in script
    assert '/archive/master' not in script
    assert '/releases/latest' not in script
    assert 'konomitv-bs4k-tscodecbridge.asd' in script
    assert 'SBLINT_SOURCE_DIR=' in script
    assert 'MALLET_BINARY=' in script
    assert re.search(r'\bcheck \\\n[ \t]+test-executable\n', script)
    assert 'test-ffmpeg-integration' in script
    assert '--help' not in script
    assert 'BUILDER_PACKAGE\\t%s\\t%s\\t%s' in script
    assert 'SOURCE_ARCHIVE_URL\\thttps://codeload.github.com/' in script
    assert 'SBLINT_SOURCE_URL\\thttps://codeload.github.com/' in script
    assert 'MALLET_ARCHIVE_URL\\thttps://github.com/' in script
    assert 'FFMPEG_ORIGIN\\tKonomiTV-BS4K-thirdparty-builder:' in script
    assert 'BUILD_GLIBC_PACKAGE_VERSION' in script
    assert 'BUILD_GLIBC_LIBRARY_SHA256' in script
    assert 'BUILD_ZLIB_PACKAGE_VERSION' in script
    assert 'BUILD_ZLIB_LIBRARY_SHA256' in script
    assert 'EXECUTABLE_SHA256' in script
    assert (
        '"ca-certificates=${KONOMITV_BS4K_TSCODECBRIDGE_CA_CERTIFICATES_PACKAGE_VERSION}"'
        in script
    )
    assert '"curl=${KONOMITV_BS4K_TSCODECBRIDGE_CURL_PACKAGE_VERSION}"' in script
    assert '"make=${KONOMITV_BS4K_TSCODECBRIDGE_MAKE_PACKAGE_VERSION}"' in script
    assert '"tar=${KONOMITV_BS4K_TSCODECBRIDGE_TAR_PACKAGE_VERSION}"' in script
    assert 'download_verified_archive "${package_url}" "${package_sha256}"' in script
    assert 'https://archive.ubuntu.com/ubuntu/pool/main/g/glibc/libc6_' in script
    assert 'https://archive.ubuntu.com/ubuntu/pool/main/z/zlib/zlib1g_' in script
    assert 'sha256sum --check --strict -' in script
    assert 'dpkg --install "${runtime_packages_root}/${output_name}"' in script
    assert 'LIBC6_PACKAGE_ARCHIVE_SHA256\\t%s' in script
    assert 'LIBC6_PACKAGE_ARCHIVE_URL\\thttps://archive.ubuntu.com/ubuntu/' in script
    assert 'ZLIB1G_PACKAGE_ARCHIVE_SHA256\\t%s' in script
    assert 'ZLIB1G_PACKAGE_ARCHIVE_URL\\thttps://archive.ubuntu.com/ubuntu/' in script


def test_bridge_archive_gate_rejects_fifo(tmp_path: Path) -> None:
    """
    固定hash後のarchiveも通常ファイルとdirectory以外を展開前に拒否することを検証する。

    Args:
        tmp_path (Path): pytest が提供する一時ディレクトリ。

    Returns:
        None
    """

    fixture_root = tmp_path / 'fixture-root'
    fixture_root.mkdir()
    (fixture_root / 'payload.txt').write_text('regular payload\n', encoding = 'utf-8')
    valid_archive = tmp_path / 'valid.tar.gz'
    subprocess.run(
        [
            'tar',
            '--create',
            '--gzip',
            '--file',
            str(valid_archive),
            '--directory',
            str(tmp_path),
            'fixture-root',
        ],
        check = True,
    )
    valid_result = subprocess.run(
        ['bash', str(BUILD_SCRIPT_PATH), 'verify-archive', str(valid_archive)],
        check = False,
        capture_output = True,
        text = True,
    )
    assert valid_result.returncode == 0, valid_result.stderr

    os.mkfifo(fixture_root / 'unsupported.fifo')
    fifo_archive = tmp_path / 'fifo.tar.gz'
    subprocess.run(
        [
            'tar',
            '--create',
            '--gzip',
            '--file',
            str(fifo_archive),
            '--directory',
            str(tmp_path),
            'fixture-root',
        ],
        check = True,
    )
    fifo_result = subprocess.run(
        ['bash', str(BUILD_SCRIPT_PATH), 'verify-archive', str(fifo_archive)],
        check = False,
        capture_output = True,
        text = True,
    )
    assert fifo_result.returncode != 0
    assert 'unsupported non-regular entry' in fifo_result.stderr


def test_dockerfile_keeps_bridge_toolchain_out_of_the_final_image() -> None:
    """
    専用 builder と最小10ファイルだけの final copy・build-time gate を検証する。

    Returns:
        None
    """

    dockerfile = DOCKERFILE_PATH.read_text(encoding = 'utf-8')
    assert dockerfile.startswith(
        f'# syntax=docker/dockerfile:1.7@sha256:{EXPECTED_DOCKERFILE_FRONTEND_SHA256}\n'
    )
    assert f'ARG UBUNTU_2204_IMAGE_SHA256={EXPECTED_UBUNTU_IMAGE_SHA256}' in dockerfile
    base = 'FROM ubuntu:22.04@sha256:${UBUNTU_2204_IMAGE_SHA256}'
    toolchain_marker = f'{base} AS tscodecbridge-toolchain'
    builder_marker = 'FROM tscodecbridge-toolchain AS tscodecbridge-builder'
    integration_marker = 'FROM thirdparty-builder AS tscodecbridge-integration'
    assert toolchain_marker in dockerfile
    assert builder_marker in dockerfile
    assert integration_marker in dockerfile
    toolchain_section = dockerfile.split(toolchain_marker, maxsplit = 1)[1].split(
        builder_marker, maxsplit = 1
    )[0]
    assert (
        'KONOMITV_BS4K_TSCODECBRIDGE_EXPECTED_UBUNTU_IMAGE_SHA256='
        '${UBUNTU_2204_IMAGE_SHA256}'
    ) in toolchain_section
    assert toolchain_section.index(
        'build.sh validate-manifest'
    ) < toolchain_section.index('build.sh prepare')

    builder_section = dockerfile.split(builder_marker, maxsplit = 1)[1].split(
        integration_marker, maxsplit = 1
    )[0]
    assert 'build.sh build' in builder_section
    assert 'Runtime-Manifest.tsv' in builder_section

    integration_section = dockerfile.split(integration_marker, maxsplit = 1)[1].split(
        '\nFROM ', maxsplit = 1
    )[0]
    assert (
        'KONOMITV_BS4K_TSCODECBRIDGE_EXPECTED_UBUNTU_IMAGE_SHA256='
        '${UBUNTU_2204_IMAGE_SHA256}'
    ) in integration_section
    assert '/build/konomitv-bs4k-tscodecbridge/source/' in integration_section
    assert '/opt/konomitv-bs4k-tscodecbridge-runtime/' in integration_section
    assert (
        'KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_ROOT=/opt/thirdparty/FFmpeg8'
        in integration_section
    )
    assert 'build.sh test-ffmpeg-integration' in integration_section
    assert dockerfile.index('build.sh build') < dockerfile.index(
        'build.sh test-ffmpeg-integration'
    )

    runtime_marker = f'{base} AS runtime'
    final_stage = runtime_marker + dockerfile.split(runtime_marker, maxsplit = 1)[1].split(
        '\nFROM ', maxsplit = 1,
    )[0]
    assert (
        'COPY --from=tscodecbridge-integration /opt/konomitv-bs4k-tscodecbridge-runtime/ '
        '\\\n    /code/server/thirdparty/KonomiTVBS4KTSCodecBridge/'
    ) in final_stage
    assert 'COPY --from=tscodecbridge-builder /build/' not in final_stage
    assert 'COPY --from=tscodecbridge-toolchain' not in final_stage
    assert (
        'RUN --mount=type=bind,from=tscodecbridge-builder,'
        'source=/opt/konomitv-bs4k-tscodecbridge-runtime-packages,'
        'target=/mnt/konomitv-bs4k-tscodecbridge-runtime-packages'
    ) in final_stage
    assert 'COPY --from=tscodecbridge-builder /opt/konomitv-bs4k-tscodecbridge-runtime-packages' not in final_stage
    for filename in [
        'ts-codec-bridge.elf',
        'LICENSE',
        'COPYRIGHT-SBCL',
        'COPYRIGHT-GLIBC',
        'COPYRIGHT-ZLIB',
        'Apache-2.0-LICENSE',
        'GPL-2-LICENSE',
        'LGPL-2.1-LICENSE',
        'GFDL-1.3-LICENSE',
        'Runtime-Manifest.tsv',
    ]:
        assert filename in final_stage
    assert 'command -v sbcl' in final_stage
    assert 'command -v mallet' in final_stage
    assert 'command -v sblint' in final_stage
    assert 'test ! -e /usr/lib/sbcl' in final_stage
    assert 'test ! -e /usr/share/sbcl-source' in final_stage
    assert 'test ! -e /root/.cache/common-lisp' in final_stage
    assert 'test ! -e /build/konomitv-bs4k-tscodecbridge' in final_stage
    assert 'test ! -e /opt/konomitv-bs4k-tscodecbridge-toolchain' in final_stage
    assert 'test ! -e /opt/konomitv-bs4k-tscodecbridge-ffmpeg8' in final_stage
    assert 'runtime_glibc_path=' in final_stage
    assert 'runtime_zlib_path=' in final_stage
    assert 'build_glibc_package_sha256=' in final_stage
    assert 'build_glibc_package_url=' in final_stage
    assert 'build_zlib_package_sha256=' in final_stage
    assert 'build_zlib_package_url=' in final_stage
    assert 'dpkg --install \\' in final_stage
    assert '"${runtime_packages_root}/libc6-amd64.deb"' in final_stage
    assert '"${runtime_packages_root}/zlib1g-amd64.deb"' in final_stage
    assert 'test -z "$(dpkg --audit)"' in final_stage
    assert (
        '''test "$(dpkg-query --showformat='${Version}' --show libc6)" = "${build_glibc_version}"'''
        in final_stage
    )
    assert (
        '''test "$(dpkg-query --showformat='${Version}' --show zlib1g)" = "${build_zlib_version}"'''
        in final_stage
    )
    assert '''"${build_glibc_sha256}" "${runtime_glibc_path}"''' in final_stage
    assert '''"${build_zlib_sha256}" "${runtime_zlib_path}"''' in final_stage
    assert '--root /code/server/thirdparty/KonomiTVBS4KTSCodecBridge' in final_stage


def test_server_bridge_path_matches_the_minimal_docker_distribution() -> None:
    """
    Server の固定実行パスが Docker final copy の保存実行形式と一致することを検証する。

    Returns:
        None
    """

    constants = (REPOSITORY_ROOT / 'server/app/constants.py').read_text(
        encoding = 'utf-8'
    )
    dockerfile = DOCKERFILE_PATH.read_text(encoding = 'utf-8')

    assert "'KonomiTVBS4KTSCodecBridge': str(" in constants
    assert "LIBRARY_DIR / 'KonomiTVBS4KTSCodecBridge/ts-codec-bridge.elf'" in constants
    assert '/code/server/thirdparty/KonomiTVBS4KTSCodecBridge/' in dockerfile
