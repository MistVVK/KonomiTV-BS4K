#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="${SOURCE_ROOT:-/build/sources}"
DOWNLOAD_ROOT="${DOWNLOAD_ROOT:-/build/downloads}"
OUTPUT_ROOT="${OUTPUT_ROOT:-/opt/thirdparty}"
TSREADEX_SOURCE="${TSREADEX_SOURCE:-/build/thirdparty-src/tsreadex}"

set -a
source "${SCRIPT_DIR}/manifest.env"
set +a

case "${INTEL_NONFREE:-}" in
    true|false) ;;
    *)
        echo "INTEL_NONFREE must be exactly 'true' or 'false'. actual: ${INTEL_NONFREE:-<unset>}" >&2
        exit 2
        ;;
esac

mkdir -p "${SOURCE_ROOT}" "${DOWNLOAD_ROOT}" "${OUTPUT_ROOT}"

download-verified() {
    local url="$1"
    local sha256="$2"
    local destination="$3"

    if [ -s "${destination}" ] && echo "${sha256}  ${destination}" | sha256sum --check --strict >/dev/null 2>&1; then
        echo "Using cached download: ${destination}"
        return 0
    fi

    rm -f "${destination}.tmp"
    curl --fail --location --retry 3 --output "${destination}.tmp" "${url}"
    mv "${destination}.tmp" "${destination}"
    echo "${sha256}  ${destination}" | sha256sum --check --strict
}

clone-commit() {
    local url="$1"
    local commit="$2"
    local destination="$3"
    local ref="${4:-${commit}}"

    git init "${destination}"
    git -C "${destination}" remote add origin "${url}"
    if [[ "${ref}" == refs/tags/* ]]; then
        git -C "${destination}" fetch --depth=1 origin "${ref}:${ref}"
        git -C "${destination}" checkout --detach "${commit}"
    else
        git -C "${destination}" fetch --depth=1 origin "${commit}"
        git -C "${destination}" checkout --detach FETCH_HEAD
    fi
    test "$(git -C "${destination}" rev-parse HEAD)" = "${commit}"
}

copy-license() {
    local source="$1"
    local destination="$2"

    test -s "${source}"
    install -m 0644 "${source}" "${destination}"
}

# ライブ・録画・メタデータ解析で共有する FFmpeg 8 を、公式ソースの固定 commit から構築する。
SOURCE_ROOT="${SOURCE_ROOT}" OUTPUT_ROOT="${OUTPUT_ROOT}" "${SCRIPT_DIR}/build-ffmpeg8.sh"

# FFmpeg 8 の QSV 経路で使う Intel Media Stack を、固定 commit と既存の修正 patch から構築する。
INTEL_NONFREE="${INTEL_NONFREE}" OUTPUT_ROOT="${SOURCE_ROOT}/intel-media-stack" \
    "${SCRIPT_DIR}/build-intel-media-stack.sh" "${SOURCE_ROOT}/intel-media-stack"
cp -a "${SOURCE_ROOT}/intel-media-stack/artifact/Library" "${OUTPUT_ROOT}/Library"

# Docker build context では submodule の Git metadata が除外されるため、固定 commit の tracked source tree と同じ内容か検証する。
actual_tsreadex_source_sha256="$(cd "${TSREADEX_SOURCE}" && { find . -type f ! -name '.git' ! -name 'tsreadex.elf' -print0 | sort -z | xargs -0 sha256sum; } | sha256sum | cut -d' ' -f1)"
test "${actual_tsreadex_source_sha256}" = "${TSREADEX_SOURCE_SHA256}"
make -C "${TSREADEX_SOURCE}" clean
make -C "${TSREADEX_SOURCE}" TARGET=tsreadex.elf -j"$(nproc)"
mkdir -p "${OUTPUT_ROOT}/tsreadex"
install -m 0755 "${TSREADEX_SOURCE}/tsreadex.elf" "${OUTPUT_ROOT}/tsreadex/tsreadex.elf"
copy-license "${TSREADEX_SOURCE}/License.txt" "${OUTPUT_ROOT}/tsreadex/License.txt"

clone-commit "${PSISIARC_REPOSITORY}" "${PSISIARC_COMMIT}" "${SOURCE_ROOT}/psisiarc"
make -C "${SOURCE_ROOT}/psisiarc" -j"$(nproc)"
mkdir -p "${OUTPUT_ROOT}/psisiarc"
install -m 0755 "${SOURCE_ROOT}/psisiarc/psisiarc" "${OUTPUT_ROOT}/psisiarc/psisiarc.elf"
copy-license "${SOURCE_ROOT}/psisiarc/License.txt" "${OUTPUT_ROOT}/psisiarc/License.txt"

# Akebi は検証済みの Go archive を展開し、固定 commit から静的リンクで構築する。
download-verified "${GO_URL}" "${GO_SHA256}" "${DOWNLOAD_ROOT}/go.tar.gz"
tar -xzf "${DOWNLOAD_ROOT}/go.tar.gz" -C /opt
clone-commit "${AKEBI_REPOSITORY}" "${AKEBI_COMMIT}" "${SOURCE_ROOT}/Akebi"
PATH="/opt/go/bin:${PATH}" make -C "${SOURCE_ROOT}/Akebi" build-https-server CGO_ENABLED=0 GOARCH=amd64
mkdir -p "${OUTPUT_ROOT}/Akebi"
install -m 0755 "${SOURCE_ROOT}/Akebi/akebi-https-server" "${OUTPUT_ROOT}/Akebi/akebi-https-server.elf"
copy-license "${SOURCE_ROOT}/Akebi/License.txt" "${OUTPUT_ROOT}/Akebi/License.txt"
copy-license /opt/go/LICENSE "${OUTPUT_ROOT}/Akebi/Go-License.txt"

# Python Standalone 自体を checksum で固定し、同梱 Python に Poetry を導入する。
download-verified "${PYTHON_URL}" "${PYTHON_SHA256}" "${DOWNLOAD_ROOT}/python.tar.gz"
tar -xzf "${DOWNLOAD_ROOT}/python.tar.gz" -C "${SOURCE_ROOT}"
mv "${SOURCE_ROOT}/python" "${OUTPUT_ROOT}/Python"
"${OUTPUT_ROOT}/Python/bin/python" -m pip install --no-cache-dir "poetry==${POETRY_VERSION}"
copy-license "${OUTPUT_ROOT}/Python/lib/python3.11/LICENSE.txt" "${OUTPUT_ROOT}/Python/License.txt"
