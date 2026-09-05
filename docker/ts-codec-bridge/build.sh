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

expected_manifest_keys=(
    KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_OWNER
    KONOMITV_BS4K_TSCODECBRIDGE_REPOSITORY_NAME
    KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT
    KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_TREE_SHA256
    KONOMITV_BS4K_TSCODECBRIDGE_CLI_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_TS_MAPPING_VERSION
    KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_CODENAME
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
    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_TREE_SHA256}" != PENDING_* ]] ||
        fail_pending 'replace SOURCE_TREE_SHA256 with the verified submodule source tree SHA-256'

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
        KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_TREE_SHA256 \
        "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_TREE_SHA256}" \
        64
    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_TREE_SHA256}" != \
        '0000000000000000000000000000000000000000000000000000000000000000' ]] ||
        fail 'SOURCE_TREE_SHA256 must not be the all-zero sentinel'
    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_CLI_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
        fail 'CLI_VERSION must be an exact semantic version'
    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_TS_MAPPING_VERSION}" =~ ^[0-9]+$ ]] ||
        fail 'TS_MAPPING_VERSION must be a decimal integer'
    [[ "${KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_CODENAME}" == 'jammy' ]] ||
        fail 'Ubuntu codename must remain jammy'
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

verify_source_tree() {
    local source_directory="$1"
    local actual_source_sha256

    [[ -d "${source_directory}" && ! -L "${source_directory}" ]] ||
        fail 'Bridge submodule source directory is missing or is a symlink'
    test -f "${source_directory}/LICENSE" || fail 'Bridge submodule source does not contain LICENSE'
    test -f "${source_directory}/konomitv-bs4k-tscodecbridge.asd" ||
        fail 'Bridge submodule source does not contain its ASDF system'

    # 従来の archive と同じく、入力は通常ファイルとディレクトリだけに限定する。
    # submodule の .git はホスト固有の管理情報なので、Docker context と内容照合の両方から除く。
    [[ -z "$(find "${source_directory}" -mindepth 1 \
        -path "${source_directory}/.git" -prune -o ! -type f ! -type d -print -quit)" ]] ||
        fail 'Bridge submodule source contains an unsupported non-regular entry'

    # Git metadata を持たない Docker 内でも、固定 commit のクリーンな tracked tree と内容を照合する。
    # 相対パスと各ファイルの SHA-256 を C locale 順で再ハッシュし、欠落・追加・変更を検出する。
    actual_source_sha256="$(
        cd -- "${source_directory}"
        find . -path './.git' -prune -o -type f -print0 |
            LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1
    )"
    [[ "${actual_source_sha256}" == "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_TREE_SHA256}" ]] ||
        fail 'Bridge submodule source tree SHA-256 does not match manifest'
    printf 'Bridge source commit: %s\n' "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT}"
    printf 'Bridge source tree SHA-256: %s\n' "${actual_source_sha256}"
}

install_toolchain() {
    local sblint_archive="${download_root}/sblint.tar.gz"
    local mallet_archive="${download_root}/mallet.tar.gz"
    local sblint_url
    local mallet_url
    local installed_mallet_version

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
    # apt package は version を固定せず公式 Jammy archive の最新を導入する。
    # 固定すると Ubuntu の security 更新で旧 version が archive から消えるたびにビルドが壊れる。
    # 導入した実測値は record_builder_packages が Runtime-Manifest.tsv へ記録する。
    apt-get install -y --no-install-recommends nala
    # libc6 / zlib1g は SBCL 製 Bridge 実行形式が動的リンクする runtime 依存なので、
    # builder と最終 image の双方で archive の最新を使い、その時点の archive 内容に揃える。
    nala install -y --no-install-recommends \
        ca-certificates \
        cl-swank \
        curl \
        libc6 \
        make \
        sbcl \
        tar \
        zlib1g
    record_builder_packages

    install -d -m 0755 \
        "${download_root}" \
        "${toolchain_root}/sblint" \
        "${toolchain_root}/mallet"
    sblint_url="https://codeload.github.com/cxxxr/sblint/tar.gz/${KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_COMMIT}"
    mallet_url="https://github.com/fukamachi/mallet/releases/download/${KONOMITV_BS4K_TSCODECBRIDGE_MALLET_VERSION}/mallet-${KONOMITV_BS4K_TSCODECBRIDGE_MALLET_VERSION}-linux-x86_64.tar.gz"

    download_verified_archive \
        "${sblint_url}" \
        "${KONOMITV_BS4K_TSCODECBRIDGE_SBLINT_SOURCE_SHA256}" \
        "${sblint_archive}"
    download_verified_archive \
        "${mallet_url}" \
        "${KONOMITV_BS4K_TSCODECBRIDGE_MALLET_ARCHIVE_SHA256}" \
        "${mallet_archive}"
    verify_single_root_archive "${sblint_archive}"
    verify_single_root_archive "${mallet_archive}"

    tar --extract --gzip --file "${sblint_archive}" \
        --directory "${toolchain_root}/sblint" --strip-components 1 --no-same-owner --no-same-permissions
    tar --extract --gzip --file "${mallet_archive}" \
        --directory "${toolchain_root}/mallet" --strip-components 1 --no-same-owner --no-same-permissions
    test -f "${toolchain_root}/sblint/sblint.asd" || fail 'SBLint archive is incomplete'
    test -x "${toolchain_root}/mallet/bin/mallet" || fail 'Mallet archive is incomplete'
    installed_mallet_version="$("${toolchain_root}/mallet/bin/mallet" --version)"
    [[ "${installed_mallet_version}" == "Mallet version ${KONOMITV_BS4K_TSCODECBRIDGE_MALLET_VERSION}" ]] ||
        fail 'Mallet version does not match manifest'

    rm -f -- "${sblint_archive}" "${mallet_archive}"
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
    local observed_cli_version
    local observed_mapping_version

    validate_manifest
    verify_source_tree "${source_root}"

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
        printf 'SOURCE_SUBMODULE_PATH\tthirdparty-src/tscodecbridge\n'
        printf 'SOURCE_COMMIT\t%s\n' "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_COMMIT}"
        printf 'SOURCE_TREE_SHA256\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_SOURCE_TREE_SHA256}"
        printf 'CLI_VERSION\t%s\n' "${KONOMITV_BS4K_TSCODECBRIDGE_CLI_VERSION}"
        printf 'TS_MAPPING_VERSION\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_TS_MAPPING_VERSION}"
        printf 'UBUNTU_CODENAME\t%s\n' \
            "${KONOMITV_BS4K_TSCODECBRIDGE_UBUNTU_CODENAME}"
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
        verify-source)
            [[ -n "${2:-}" ]] || fail 'usage: build.sh verify-source DIRECTORY'
            validate_manifest
            verify_source_tree "$2"
            ;;
        prepare)
            install_toolchain
            ;;
        build)
            build_and_package
            ;;
        test-ffmpeg-integration)
            run_ffmpeg_integration
            ;;
        *)
            fail 'usage: build.sh {validate-manifest|verify-archive|verify-source|prepare|build|test-ffmpeg-integration}'
            ;;
    esac
}

main "$@"
