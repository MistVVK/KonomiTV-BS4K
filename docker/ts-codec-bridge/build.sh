#!/usr/bin/env bash

set -euo pipefail

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
manifest_path="${KONOMITV_BS4K_TSCODECBRIDGE_MANIFEST_PATH:-${script_directory}/manifest.env}"
build_root='/build/konomitv-bs4k-tscodecbridge'
source_root="${build_root}/source"
download_root="${build_root}/downloads"
initial_packages_path="${build_root}/packages-before.tsv"
builder_packages_path="${build_root}/builder-packages.tsv"
toolchain_root='/opt/konomitv-bs4k-tscodecbridge-toolchain'
ffmpeg_root="${KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_ROOT:-/opt/konomitv-bs4k-tscodecbridge-ffmpeg8}"
runtime_root='/opt/konomitv-bs4k-tscodecbridge-runtime'
runtime_packages_root='/opt/konomitv-bs4k-tscodecbridge-runtime-packages'

expected_manifest_keys=(
    KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_OWNER
    KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_NAME
    KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT
    KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_ARCHIVE_SHA256
    KONOMITV_BS4K_TSCODECBRIDGE_CLI_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_TS_MAPPING_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_CODENAME
    KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_IMAGE_SHA256
    KONOMITV_BS4K_TSCODECBRIDGE_CA_CERTIFICATES_PACKAGE_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_CURL_PACKAGE_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_MAKE_PACKAGE_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_TAR_PACKAGE_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_NALA_PACKAGE_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_SBCL_PACKAGE_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_CL_SWANK_PACKAGE_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_SHA256
    KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_SHA256
    KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_COMMIT
    KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_SOURCE_SHA256
    KONOMITV_BS4K_TSCODECBRIDGE_MALLET_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_MALLET_ARCHIVE_SHA256
    KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_BINARY_SHA256
    KONOMITV_BS4K_TSCODECBRIDGE_FFPROBE_BINARY_SHA256
)

fail() {
    printf 'KonomiTV-BS4K TS Codec Bridge build error: %s\n' "$*" >&2
    exit 1
}

fail_pending() {
    printf 'KonomiTV-BS4K TS Codec Bridge reference is pending: %s\n' "$*" >&2
    exit 78
}

is_expected_manifest_key() {
    local candidate="$1"
    local expected

    for expected in "${expected_manifest_keys[@]}"; do
        if [[ "${candidate}" == "${expected}" ]]; then
            return 0
        fi
    done
    return 1
}

load_manifest() {
    local line
    local key
    local value
    local expected
    declare -A seen_keys=()

    test -f "${manifest_path}" || fail "manifest is missing: ${manifest_path}"
    while IFS= read -r line || [[ -n "${line}" ]]; do
        if [[ -z "${line}" || "${line}" == \#* ]]; then
            continue
        fi
        if [[ ! "${line}" =~ ^([A-Z0-9_]+)=([A-Za-z0-9._:+~-]+)$ ]]; then
            fail "manifest contains an unsafe or malformed line: ${line}"
        fi
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        is_expected_manifest_key "${key}" || fail "manifest contains an unknown key: ${key}"
        [[ -z "${seen_keys[${key}]+x}" ]] || fail "manifest contains a duplicate key: ${key}"
        printf -v "${key}" '%s' "${value}"
        export "${key?}"
        seen_keys["${key}"]=1
    done < "${manifest_path}"

    for expected in "${expected_manifest_keys[@]}"; do
        [[ -n "${seen_keys[${expected}]+x}" ]] || fail "manifest key is missing: ${expected}"
    done
    [[ "${#seen_keys[@]}" -eq "${#expected_manifest_keys[@]}" ]] ||
        fail 'manifest key count does not match the fixed schema'
}

require_lower_hex() {
    local field_name="$1"
    local value="$2"
    local length="$3"

    [[ "${value}" =~ ^[0-9a-f]+$ ]] || fail "${field_name} must contain lowercase hexadecimal characters only"
    [[ "${#value}" -eq "${length}" ]] || fail "${field_name} must be exactly ${length} characters"
}

validate_manifest() {
    load_manifest

    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT}" != PENDING_* ]] ||
        fail_pending 'replace SOURCE_COMMIT with the published 40-character commit SHA'
    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_ARCHIVE_SHA256}" != PENDING_* ]] ||
        fail_pending 'replace SOURCE_ARCHIVE_SHA256 with the verified codeload archive SHA-256'

    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_OWNER}" == 'MistVVK' ]] ||
        fail 'repository owner must remain MistVVK'
    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_NAME}" == 'KonomiTV-BS4K-TSCodecBridge' ]] ||
        fail 'repository name must remain KonomiTV-BS4K-TSCodecBridge'
    require_lower_hex \
        KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT \
        "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT}" \
        40
    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT}" != \
        '0000000000000000000000000000000000000000' ]] ||
        fail 'SOURCE_COMMIT must not be the all-zero sentinel'
    require_lower_hex \
        KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_ARCHIVE_SHA256 \
        "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_ARCHIVE_SHA256}" \
        64
    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_ARCHIVE_SHA256}" != \
        '0000000000000000000000000000000000000000000000000000000000000000' ]] ||
        fail 'SOURCE_ARCHIVE_SHA256 must not be the all-zero sentinel'
    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_CLI_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
        fail 'CLI_VERSION must be an exact semantic version'
    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_TS_MAPPING_VERSION}" =~ ^[0-9]+$ ]] ||
        fail 'TS_MAPPING_VERSION must be a decimal integer'
    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_CODENAME}" == 'jammy' ]] ||
        fail 'Ubuntu codename must remain jammy'
    require_lower_hex \
        KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_IMAGE_SHA256 \
        "${KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_IMAGE_SHA256}" \
        64
    require_lower_hex \
        KONOMITV_BS4K_TSCODECBRIDGE_EXPECTED_UBUNTU_IMAGE_SHA256 \
        "${KONOMITV_BS4K_TSCODECBRIDGE_EXPECTED_UBUNTU_IMAGE_SHA256:-}" \
        64
    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_IMAGE_SHA256}" == \
        "${KONOMITV_BS4K_TSCODECBRIDGE_EXPECTED_UBUNTU_IMAGE_SHA256}" ]] ||
        fail 'manifest Ubuntu image digest does not match the Docker base digest'
    require_lower_hex \
        KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_SHA256 \
        "${KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_SHA256}" \
        64
    require_lower_hex \
        KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_SHA256 \
        "${KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_SHA256}" \
        64
    require_lower_hex \
        KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_COMMIT \
        "${KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_COMMIT}" \
        40
    require_lower_hex \
        KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_SOURCE_SHA256 \
        "${KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_SOURCE_SHA256}" \
        64
    require_lower_hex \
        KONOMITV_BS4K_TSCODECBRIDGE_MALLET_ARCHIVE_SHA256 \
        "${KONOMITV_BS4K_TSCODECBRIDGE_MALLET_ARCHIVE_SHA256}" \
        64
    require_lower_hex \
        KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_BINARY_SHA256 \
        "${KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_BINARY_SHA256}" \
        64
    require_lower_hex \
        KONOMITV_BS4K_TSCODECBRIDGE_FFPROBE_BINARY_SHA256 \
        "${KONOMITV_BS4K_TSCODECBRIDGE_FFPROBE_BINARY_SHA256}" \
        64
}

verify_official_ubuntu_sources() {
    local source_line
    local sources=()

    while IFS= read -r source_line; do
        sources+=("${source_line}")
    done < <(
        grep -RhsE '^[[:space:]]*deb[[:space:]]' \
            /etc/apt/sources.list \
            /etc/apt/sources.list.d 2>/dev/null ||
            true
    )
    [[ "${#sources[@]}" -gt 0 ]] || fail 'no Ubuntu apt source is configured'
    for source_line in "${sources[@]}"; do
        [[ "${source_line}" =~ https?://(archive|security)\.ubuntu\.com/ubuntu/?([[:space:]]|$) ]] ||
            fail "non-official apt source is forbidden in the Bridge builder: ${source_line}"
    done
}

download_verified_archive() {
    local url="$1"
    local expected_sha256="$2"
    local output_path="$3"

    curl \
        --proto '=https' \
        --tlsv1.2 \
        --fail \
        --location \
        --silent \
        --show-error \
        --retry 3 \
        --retry-all-errors \
        "${url}" \
        --output "${output_path}"
    printf '%s  %s\n' "${expected_sha256}" "${output_path}" |
        sha256sum --check --strict -
}

download_and_install_fixed_runtime_package() {
    local package_name="$1"
    local package_version="$2"
    local package_sha256="$3"
    local output_name="$4"
    local package_url="$5"
    local package_download_root="${download_root}/runtime-packages/${package_name}"
    local downloaded_archive="${package_download_root}/${output_name}"

    test ! -e "${package_download_root}" ||
        fail "runtime package download directory already exists: ${package_download_root}"
    install -d -m 0755 "${package_download_root}" "${runtime_packages_root}"
    download_verified_archive "${package_url}" "${package_sha256}" "${downloaded_archive}"

    [[ "$(dpkg-deb --field "${downloaded_archive}" Package)" == "${package_name}" ]] ||
        fail "downloaded package name does not match manifest: ${package_name}"
    [[ "$(dpkg-deb --field "${downloaded_archive}" Version)" == "${package_version}" ]] ||
        fail "downloaded package version does not match manifest: ${package_name}"
    [[ "$(dpkg-deb --field "${downloaded_archive}" Architecture)" == 'amd64' ]] ||
        fail "downloaded package architecture is not amd64: ${package_name}"

    install -m 0644 "${downloaded_archive}" "${runtime_packages_root}/${output_name}"
    dpkg --install "${runtime_packages_root}/${output_name}"
    [[ "$(dpkg-query --showformat='${Version}' --show "${package_name}")" == "${package_version}" ]] ||
        fail "installed runtime package version does not match manifest: ${package_name}"
    printf 'Fixed runtime package: %s %s (%s)\n' \
        "${package_name}" \
        "${package_version}" \
        "${package_url}"
}

verify_single_root_archive() {
    local archive_path="$1"

    if ! LC_ALL=C tar --list --verbose --gzip --file "${archive_path}" |
        awk '
            {
                entry_type = substr($1, 1, 1)
                if (entry_type != "-" && entry_type != "d") {
                    invalid = 1
                }
            }
            END {
                exit invalid ? 1 : 0
            }
        '
    then
        fail "archive contains an unsupported non-regular entry: ${archive_path}"
    fi
    tar --list --gzip --file "${archive_path}" |
        awk '
            BEGIN {
                invalid = 0
            }
            /^\// || /(^|\/)\.\.(\/|$)/ {
                invalid = 1
            }
            {
                split($0, path, "/")
                if (path[1] != "") {
                    roots[path[1]] = 1
                }
            }
            END {
                root_count = 0
                for (root in roots) {
                    root_count += 1
                }
                if (invalid || root_count != 1) {
                    exit 1
                }
            }
        ' ||
        fail "archive must contain one safe top-level directory: ${archive_path}"
}

record_builder_packages() {
    local installed_packages_path="${build_root}/packages-after.tsv"
    local changed_packages_path="${build_root}/packages-changed.tsv"
    local package
    local version
    local origin

    dpkg-query \
        --show \
        --showformat='${binary:Package}\t${Version}\n' |
        sort > "${installed_packages_path}"
    awk -F '\t' '
        NR == FNR {
            before[$1] = $2
            next
        }
        !($1 in before) || before[$1] != $2 {
            print $1 "\t" $2
        }
    ' "${initial_packages_path}" "${installed_packages_path}" > "${changed_packages_path}"
    test -s "${changed_packages_path}" ||
        fail 'builder package installation unexpectedly changed no package'

    : > "${builder_packages_path}"
    while IFS=$'\t' read -r package version; do
        origin="$(
            apt-cache policy "${package}" |
                awk -v installed_version="${version}" '
                    $1 == "***" && $2 == installed_version {
                        selected = 1
                        next
                    }
                    selected && $1 ~ /^[0-9]+$/ && $2 ~ /^https?:\/\// {
                        print $2 " " $3 " " $4 " " $5
                        exit
                    }
                '
        )"
        [[ "${origin}" =~ ^https?://(archive|security)\.ubuntu\.com/ubuntu[[:space:]] ]] ||
            fail "builder package does not have an official Ubuntu archive origin: ${package} ${version}"
        printf 'BUILDER_PACKAGE\t%s\t%s\t%s\n' \
            "${package}" \
            "${version}" \
            "${origin}" |
            tee -a "${builder_packages_path}"
    done < "${changed_packages_path}"
}

install_toolchain_and_source() {
    local bridge_archive="${download_root}/tscodecbridge.tar.gz"
    local sblint_archive="${download_root}/sblint.tar.gz"
    local mallet_archive="${download_root}/mallet.tar.gz"
    local bridge_url
    local sblint_url
    local mallet_url
    local package
    local installed_sbcl_version
    local installed_cl_swank_version
    local installed_mallet_version
    local installed_nala_version
    local installed_ca_certificates_version
    local installed_curl_version
    local installed_make_version
    local installed_tar_version
    local installed_libc6_version
    local installed_zlib1g_version

    validate_manifest
    [[ "$(uname -m)" == 'x86_64' ]] || fail 'Bridge builder supports Linux x86_64 only'
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ "${VERSION_CODENAME}" == "${KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_CODENAME}" ]] ||
        fail "unexpected Ubuntu codename: ${VERSION_CODENAME}"
    verify_official_ubuntu_sources

    install -d -m 0755 "${build_root}" "${download_root}"
    dpkg-query \
        --show \
        --showformat='${binary:Package}\t${Version}\n' |
        sort > "${initial_packages_path}"
    apt-get update
    apt-get install -y --no-install-recommends \
        "nala=${KONOMITV_BS4K_TSCODECBRIDGE_NALA_PACKAGE_VERSION}"
    nala install -y --no-install-recommends \
        "ca-certificates=${KONOMITV_BS4K_TSCODECBRIDGE_CA_CERTIFICATES_PACKAGE_VERSION}" \
        "cl-swank=${KONOMITV_BS4K_TSCODECBRIDGE_CL_SWANK_PACKAGE_VERSION}" \
        "curl=${KONOMITV_BS4K_TSCODECBRIDGE_CURL_PACKAGE_VERSION}" \
        "make=${KONOMITV_BS4K_TSCODECBRIDGE_MAKE_PACKAGE_VERSION}" \
        "sbcl=${KONOMITV_BS4K_TSCODECBRIDGE_SBCL_PACKAGE_VERSION}" \
        "tar=${KONOMITV_BS4K_TSCODECBRIDGE_TAR_PACKAGE_VERSION}"
    download_and_install_fixed_runtime_package \
        libc6 \
        "${KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_VERSION}" \
        "${KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_SHA256}" \
        libc6-amd64.deb \
        "https://archive.ubuntu.com/ubuntu/pool/main/g/glibc/libc6_${KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_VERSION}_amd64.deb"
    download_and_install_fixed_runtime_package \
        zlib1g \
        "${KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_VERSION}" \
        "${KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_SHA256}" \
        zlib1g-amd64.deb \
        "https://archive.ubuntu.com/ubuntu/pool/main/z/zlib/zlib1g_${KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_VERSION#*:}_amd64.deb"

    for package in sbcl cl-swank; do
        printf 'Ubuntu package policy for %s:\n' "${package}"
        apt-cache policy "${package}"
        apt-cache policy "${package}" |
            grep -Eq 'https?://archive\.ubuntu\.com/ubuntu[[:space:]]+jammy/universe[[:space:]]+amd64[[:space:]]+Packages' ||
            fail "${package} was not selected from the official Jammy universe archive"
    done
    installed_nala_version="$(dpkg-query --showformat='${Version}' --show nala)"
    installed_ca_certificates_version="$(
        dpkg-query --showformat='${Version}' --show ca-certificates
    )"
    installed_curl_version="$(dpkg-query --showformat='${Version}' --show curl)"
    installed_make_version="$(dpkg-query --showformat='${Version}' --show make)"
    installed_tar_version="$(dpkg-query --showformat='${Version}' --show tar)"
    installed_sbcl_version="$(dpkg-query --showformat='${Version}' --show sbcl)"
    installed_cl_swank_version="$(dpkg-query --showformat='${Version}' --show cl-swank)"
    installed_libc6_version="$(dpkg-query --showformat='${Version}' --show libc6)"
    installed_zlib1g_version="$(dpkg-query --showformat='${Version}' --show zlib1g)"
    [[ "${installed_nala_version}" == "${KONOMITV_BS4K_TSCODECBRIDGE_NALA_PACKAGE_VERSION}" ]] ||
        fail 'installed nala package version does not match manifest'
    [[ "${installed_ca_certificates_version}" == \
        "${KONOMITV_BS4K_TSCODECBRIDGE_CA_CERTIFICATES_PACKAGE_VERSION}" ]] ||
        fail 'installed ca-certificates package version does not match manifest'
    [[ "${installed_curl_version}" == "${KONOMITV_BS4K_TSCODECBRIDGE_CURL_PACKAGE_VERSION}" ]] ||
        fail 'installed curl package version does not match manifest'
    [[ "${installed_make_version}" == "${KONOMITV_BS4K_TSCODECBRIDGE_MAKE_PACKAGE_VERSION}" ]] ||
        fail 'installed make package version does not match manifest'
    [[ "${installed_tar_version}" == "${KONOMITV_BS4K_TSCODECBRIDGE_TAR_PACKAGE_VERSION}" ]] ||
        fail 'installed tar package version does not match manifest'
    [[ "${installed_sbcl_version}" == "${KONOMITV_BS4K_TSCODECBRIDGE_SBCL_PACKAGE_VERSION}" ]] ||
        fail 'installed SBCL package version does not match manifest'
    [[ "${installed_cl_swank_version}" == "${KONOMITV_BS4K_TSCODECBRIDGE_CL_SWANK_PACKAGE_VERSION}" ]] ||
        fail 'installed cl-swank package version does not match manifest'
    [[ "${installed_libc6_version}" == "${KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_VERSION}" ]] ||
        fail 'installed libc6 package version does not match manifest'
    [[ "${installed_zlib1g_version}" == "${KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_VERSION}" ]] ||
        fail 'installed zlib1g package version does not match manifest'
    dpkg-query \
        --show \
        --showformat='Ubuntu package: ${Package} ${Version} (${source:Package})\n' \
        sbcl cl-swank
    record_builder_packages

    install -d -m 0755 \
        "${download_root}" \
        "${source_root}" \
        "${toolchain_root}/sblint" \
        "${toolchain_root}/mallet"
    bridge_url="https://codeload.github.com/${KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_OWNER}/${KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_NAME}/tar.gz/${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT}"
    sblint_url="https://codeload.github.com/cxxxr/sblint/tar.gz/${KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_COMMIT}"
    mallet_url="https://github.com/fukamachi/mallet/releases/download/${KONOMITV_BS4K_TSCODECBRIDGE_MALLET_VERSION}/mallet-${KONOMITV_BS4K_TSCODECBRIDGE_MALLET_VERSION}-linux-x86_64.tar.gz"

    download_verified_archive \
        "${bridge_url}" \
        "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_ARCHIVE_SHA256}" \
        "${bridge_archive}"
    download_verified_archive \
        "${sblint_url}" \
        "${KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_SOURCE_SHA256}" \
        "${sblint_archive}"
    download_verified_archive \
        "${mallet_url}" \
        "${KONOMITV_BS4K_TSCODECBRIDGE_MALLET_ARCHIVE_SHA256}" \
        "${mallet_archive}"
    verify_single_root_archive "${bridge_archive}"
    verify_single_root_archive "${sblint_archive}"
    verify_single_root_archive "${mallet_archive}"

    tar --extract --gzip --file "${bridge_archive}" \
        --directory "${source_root}" --strip-components 1 --no-same-owner --no-same-permissions
    tar --extract --gzip --file "${sblint_archive}" \
        --directory "${toolchain_root}/sblint" --strip-components 1 --no-same-owner --no-same-permissions
    tar --extract --gzip --file "${mallet_archive}" \
        --directory "${toolchain_root}/mallet" --strip-components 1 --no-same-owner --no-same-permissions
    test -f "${source_root}/LICENSE" || fail 'Bridge source archive does not contain LICENSE'
    test -f "${source_root}/konomitv-bs4k-tscodecbridge.asd" ||
        fail 'Bridge source archive does not contain its ASDF system'
    test -f "${toolchain_root}/sblint/sblint.asd" || fail 'SBLint archive is incomplete'
    test -x "${toolchain_root}/mallet/bin/mallet" || fail 'Mallet archive is incomplete'
    installed_mallet_version="$("${toolchain_root}/mallet/bin/mallet" --version)"
    [[ "${installed_mallet_version}" == "Mallet version ${KONOMITV_BS4K_TSCODECBRIDGE_MALLET_VERSION}" ]] ||
        fail 'Mallet version does not match manifest'

    printf 'Bridge source commit: %s\n' "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT}"
    printf 'Bridge source archive SHA-256: %s\n' \
        "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_ARCHIVE_SHA256}"
    rm -f -- "${bridge_archive}" "${sblint_archive}" "${mallet_archive}"
    apt-get clean
    rm -rf -- /var/lib/apt/lists/*
}

verify_bridge_dependencies() {
    local bridge_binary="$1"
    local dependency
    local dependency_names

    dependency_names="$(
        ldd "${bridge_binary}" |
            awk '
                /=>/ {
                    print $1
                    next
                }
                /^[[:space:]]*\// {
                    path = $1
                    sub(/^.*\//, "", path)
                    print path
                }
            ' |
            sort -u
    )"
    if ldd "${bridge_binary}" | grep -Fq 'not found'; then
        fail 'Bridge executable has an unresolved dynamic dependency'
    fi
    while IFS= read -r dependency; do
        [[ -z "${dependency}" ]] && continue
        case "${dependency}" in
            ld-linux-x86-64.so.2 | libc.so.6 | libdl.so.2 | libm.so.6 | libpthread.so.0 | libz.so.1)
                ;;
            *)
                fail "Bridge executable has an unexpected dynamic dependency: ${dependency}"
                ;;
        esac
    done <<< "${dependency_names}"
}

verify_fixed_ffmpeg() {
    local ffmpeg_binary="${ffmpeg_root}/ffmpeg8.elf"
    local ffprobe_binary="${ffmpeg_root}/ffprobe8.elf"
    local observed_ffmpeg_version
    local observed_ffprobe_version

    test -x "${ffmpeg_binary}" || fail 'fixed thirdparty-builder FFmpeg 8 is missing'
    test -x "${ffprobe_binary}" || fail 'fixed thirdparty-builder ffprobe 8 is missing'
    printf '%s  %s\n' \
        "${KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_BINARY_SHA256}" \
        "${ffmpeg_binary}" |
        sha256sum --check --strict -
    printf '%s  %s\n' \
        "${KONOMITV_BS4K_TSCODECBRIDGE_FFPROBE_BINARY_SHA256}" \
        "${ffprobe_binary}" |
        sha256sum --check --strict -
    observed_ffmpeg_version="$("${ffmpeg_binary}" -version 2>&1 | sed -n '1p')"
    observed_ffprobe_version="$("${ffprobe_binary}" -version 2>&1 | sed -n '1p')"
    [[ "${observed_ffmpeg_version}" == \
        "ffmpeg version ${KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_VERSION} "* ]] ||
        fail 'fixed thirdparty-builder FFmpeg version does not match manifest'
    [[ "${observed_ffprobe_version}" == \
        "ffprobe version ${KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_VERSION} "* ]] ||
        fail 'fixed thirdparty-builder ffprobe version does not match manifest'
}

run_ffmpeg_integration() {
    local bridge_binary="${runtime_root}/ts-codec-bridge.elf"
    local ffmpeg_binary="${ffmpeg_root}/ffmpeg8.elf"
    local ffprobe_binary="${ffmpeg_root}/ffprobe8.elf"

    validate_manifest
    test -d "${source_root}" || fail 'Bridge source was not copied into the integration stage'
    test -x "${bridge_binary}" || fail 'built Bridge executable is missing from the integration stage'
    verify_fixed_ffmpeg
    [[ "$("${bridge_binary}" --version)" == "${KONOMITV_BS4K_TSCODECBRIDGE_CLI_VERSION}" ]] ||
        fail 'Bridge CLI version changed before FFmpeg integration'
    [[ "$("${bridge_binary}" --mapping-version)" == \
        "${KONOMITV_BS4K_TSCODECBRIDGE_TS_MAPPING_VERSION}" ]] ||
        fail 'Bridge TS mapping version changed before FFmpeg integration'

    make \
        --directory "${source_root}" \
        BRIDGE_BINARY="${bridge_binary}" \
        FFMPEG_BINARY="${ffmpeg_binary}" \
        FFPROBE_BINARY="${ffprobe_binary}" \
        FFMPEG_SHA256="${KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_BINARY_SHA256}" \
        FFPROBE_SHA256="${KONOMITV_BS4K_TSCODECBRIDGE_FFPROBE_BINARY_SHA256}" \
        test-ffmpeg-integration
}

build_and_package() {
    local bridge_binary="${source_root}/build/ts-codec-bridge.elf"
    local executable_sha256
    local runtime_manifest
    local glibc_package_version
    local glibc_library_sha256
    local zlib_package_version
    local zlib_library_sha256
    local observed_cli_version
    local observed_mapping_version

    validate_manifest
    test -d "${source_root}" || fail 'Bridge source was not prepared'

    printf 'Building Bridge source commit: %s\n' \
        "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT}"
    make \
        --directory "${source_root}" \
        SBLINT_SOURCE_DIR="${toolchain_root}/sblint" \
        MALLET_BINARY="${toolchain_root}/mallet/bin/mallet" \
        check \
        test-executable

    observed_cli_version="$("${bridge_binary}" --version)"
    observed_mapping_version="$("${bridge_binary}" --mapping-version)"
    [[ "${observed_cli_version}" == "${KONOMITV_BS4K_TSCODECBRIDGE_CLI_VERSION}" ]] ||
        fail 'Bridge CLI version does not match manifest'
    [[ "${observed_mapping_version}" == "${KONOMITV_BS4K_TSCODECBRIDGE_TS_MAPPING_VERSION}" ]] ||
        fail 'Bridge TS mapping version does not match manifest'
    verify_bridge_dependencies "${bridge_binary}"
    executable_sha256="$(sha256sum "${bridge_binary}" | cut -d' ' -f1)"
    printf 'Bridge executable SHA-256: %s\n' "${executable_sha256}"
    glibc_package_version="$(dpkg-query --showformat='${Version}' --show libc6)"
    glibc_library_sha256="$(sha256sum /lib/x86_64-linux-gnu/libc.so.6 | cut -d' ' -f1)"
    zlib_package_version="$(dpkg-query --showformat='${Version}' --show zlib1g)"
    zlib_library_sha256="$(sha256sum /lib/x86_64-linux-gnu/libz.so.1 | cut -d' ' -f1)"
    test -s "${builder_packages_path}" || fail 'builder package manifest is missing'

    test ! -e "${runtime_root}" || fail "runtime output already exists: ${runtime_root}"
    install -d -m 0755 "${runtime_root}"
    install -m 0755 "${bridge_binary}" "${runtime_root}/ts-codec-bridge.elf"
    install -m 0644 "${source_root}/LICENSE" "${runtime_root}/LICENSE"
    install -m 0644 /usr/share/doc/sbcl/copyright "${runtime_root}/COPYRIGHT-SBCL"
    install -m 0644 /usr/share/doc/libc6/copyright "${runtime_root}/COPYRIGHT-GLIBC"
    install -m 0644 /usr/share/doc/zlib1g/copyright "${runtime_root}/COPYRIGHT-ZLIB"
    for common_license in Apache-2.0 GPL-2 LGPL-2.1 GFDL-1.3; do
        common_license_path="/usr/share/common-licenses/${common_license}"
        test -s "${common_license_path}" ||
            fail "common license text is missing: ${common_license_path}"
        install -m 0644 "${common_license_path}" \
            "${runtime_root}/${common_license}-LICENSE"
    done

    runtime_manifest="${runtime_root}/Runtime-Manifest.tsv"
    {
        printf 'SOURCE_REPOSITORY\thttps://github.com/%s/%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_OWNER}" \
            "${KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_NAME}"
        printf 'SOURCE_ARCHIVE_URL\thttps://codeload.github.com/%s/%s/tar.gz/%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_OWNER}" \
            "${KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_NAME}" \
            "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT}"
        printf 'SOURCE_COMMIT\t%s\n' "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT}"
        printf 'SOURCE_ARCHIVE_SHA256\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_ARCHIVE_SHA256}"
        printf 'CLI_VERSION\t%s\n' "${KONOMITV_BS4K_TSCODECBRIDGE_CLI_VERSION}"
        printf 'TS_MAPPING_VERSION\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_TS_MAPPING_VERSION}"
        printf 'UBUNTU_CODENAME\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_CODENAME}"
        printf 'UBUNTU_IMAGE_SHA256\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_IMAGE_SHA256}"
        printf 'CA_CERTIFICATES_PACKAGE_VERSION\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_CA_CERTIFICATES_PACKAGE_VERSION}"
        printf 'CURL_PACKAGE_VERSION\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_CURL_PACKAGE_VERSION}"
        printf 'MAKE_PACKAGE_VERSION\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_MAKE_PACKAGE_VERSION}"
        printf 'TAR_PACKAGE_VERSION\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_TAR_PACKAGE_VERSION}"
        printf 'NALA_PACKAGE_VERSION\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_NALA_PACKAGE_VERSION}"
        printf 'SBCL_PACKAGE_VERSION\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_SBCL_PACKAGE_VERSION}"
        printf 'CL_SWANK_PACKAGE_VERSION\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_CL_SWANK_PACKAGE_VERSION}"
        printf 'LIBC6_PACKAGE_ARCHIVE_SHA256\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_SHA256}"
        printf 'LIBC6_PACKAGE_ARCHIVE_URL\thttps://archive.ubuntu.com/ubuntu/pool/main/g/glibc/libc6_%s_amd64.deb\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_LIBC6_PACKAGE_VERSION}"
        printf 'ZLIB1G_PACKAGE_ARCHIVE_SHA256\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_SHA256}"
        printf 'ZLIB1G_PACKAGE_ARCHIVE_URL\thttps://archive.ubuntu.com/ubuntu/pool/main/z/zlib/zlib1g_%s_amd64.deb\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_ZLIB1G_PACKAGE_VERSION#*:}"
        printf 'SBLINT_COMMIT\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_COMMIT}"
        printf 'SBLINT_SOURCE_SHA256\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_SOURCE_SHA256}"
        printf 'SBLINT_SOURCE_URL\thttps://codeload.github.com/cxxxr/sblint/tar.gz/%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_COMMIT}"
        printf 'MALLET_VERSION\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_MALLET_VERSION}"
        printf 'MALLET_ARCHIVE_SHA256\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_MALLET_ARCHIVE_SHA256}"
        printf 'MALLET_ARCHIVE_URL\thttps://github.com/fukamachi/mallet/releases/download/%s/mallet-%s-linux-x86_64.tar.gz\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_MALLET_VERSION}" \
            "${KONOMITV_BS4K_TSCODECBRIDGE_MALLET_VERSION}"
        printf 'FFMPEG_ORIGIN\tKonomiTV-BS4K-thirdparty-builder:/opt/thirdparty/FFmpeg8\n'
        printf 'FFMPEG_VERSION\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_VERSION}"
        printf 'FFMPEG_BINARY_SHA256\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_BINARY_SHA256}"
        printf 'FFPROBE_BINARY_SHA256\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_FFPROBE_BINARY_SHA256}"
        printf 'BUILD_GLIBC_PACKAGE_VERSION\t%s\n' "${glibc_package_version}"
        printf 'BUILD_GLIBC_LIBRARY_SHA256\t%s\n' "${glibc_library_sha256}"
        printf 'BUILD_ZLIB_PACKAGE_VERSION\t%s\n' "${zlib_package_version}"
        printf 'BUILD_ZLIB_LIBRARY_SHA256\t%s\n' "${zlib_library_sha256}"
        printf 'EXECUTABLE_SHA256\t%s\n' "${executable_sha256}"
        for common_license in Apache-2.0 GPL-2 LGPL-2.1 GFDL-1.3; do
            common_license_path="/usr/share/common-licenses/${common_license}"
            printf 'COMMON_LICENSE\t%s-LICENSE\t%s\t%s\n' \
                "${common_license}" \
                "$(sha256sum "${common_license_path}" | cut -d' ' -f1)" \
                "${common_license_path}"
        done
        cat "${builder_packages_path}"
    } > "${runtime_manifest}"
    chmod 0644 "${runtime_manifest}"
}

main() {
    local command="${1:-}"

    case "${command}" in
        validate-manifest)
            validate_manifest
            ;;
        verify-archive)
            [[ -n "${2:-}" ]] || fail 'usage: build.sh verify-archive ARCHIVE'
            verify_single_root_archive "$2"
            ;;
        prepare)
            install_toolchain_and_source
            ;;
        build)
            build_and_package
            ;;
        test-ffmpeg-integration)
            run_ffmpeg_integration
            ;;
        *)
            fail 'usage: build.sh {validate-manifest|verify-archive|prepare|build|test-ffmpeg-integration}'
            ;;
    esac
}

main "$@"
