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

# FFmpeg は短期保持の外部 autobuild に依存せず、公式ソースの完全 commit から構築する。
clone-commit "${FFMPEG_REPOSITORY}" "${FFMPEG_COMMIT}" "${SOURCE_ROOT}/ffmpeg" "refs/tags/${FFMPEG_TAG}"
pushd "${SOURCE_ROOT}/ffmpeg"
./configure \
    --prefix=/opt/ffmpeg \
    --cc='ccache gcc' \
    --cxx='ccache g++' \
    --disable-autodetect \
    --disable-debug \
    --disable-doc \
    --disable-ffplay \
    --enable-gpl \
    --enable-version3 \
    --enable-shared \
    --disable-static \
    --enable-libopus \
    --enable-libx264 \
    --enable-libx265 \
    --enable-zlib \
    --extra-ldflags='-Wl,-rpath,$ORIGIN'
make -j"$(nproc)"
make install
popd

mkdir -p "${OUTPUT_ROOT}/FFmpeg"
install -m 0755 /opt/ffmpeg/bin/ffmpeg "${OUTPUT_ROOT}/FFmpeg/ffmpeg.elf"
install -m 0755 /opt/ffmpeg/bin/ffprobe "${OUTPUT_ROOT}/FFmpeg/ffprobe.elf"
cp -a /opt/ffmpeg/lib/libav*.so* /opt/ffmpeg/lib/libpostproc.so* /opt/ffmpeg/lib/libsw*.so* "${OUTPUT_ROOT}/FFmpeg/"
copy-license "${SOURCE_ROOT}/ffmpeg/LICENSE.md" "${OUTPUT_ROOT}/FFmpeg/License.txt"
copy-license "${SOURCE_ROOT}/ffmpeg/COPYING.GPLv3" "${OUTPUT_ROOT}/FFmpeg/COPYING.GPLv3"
find "${OUTPUT_ROOT}/FFmpeg" -type f -name '*.so*' -exec patchelf --set-rpath '$ORIGIN' {} +
patchelf --set-rpath '$ORIGIN' "${OUTPUT_ROOT}/FFmpeg/ffmpeg.elf"
patchelf --set-rpath '$ORIGIN' "${OUTPUT_ROOT}/FFmpeg/ffprobe.elf"

# 既存 FFmpeg 7 とは共有ライブラリも含めて分離し、将来の HW 処理向け FFmpeg 8 を構築する。
SOURCE_ROOT="${SOURCE_ROOT}" OUTPUT_ROOT="${OUTPUT_ROOT}" "${SCRIPT_DIR}/build-ffmpeg8.sh"

# encoder の deb とライセンスは同一 release tag に対応する完全 commit へ固定する。
for encoder in QSVENCC NVENCC VCEENCC; do
    version_variable="${encoder}_VERSION"
    url_variable="${encoder}_URL"
    sha256_variable="${encoder}_SHA256"
    repository_variable="${encoder}_REPOSITORY"
    commit_variable="${encoder}_COMMIT"
    lowercase_name="$(echo "${encoder}" | tr '[:upper:]' '[:lower:]')"
    download_path="${DOWNLOAD_ROOT}/${lowercase_name}_${!version_variable}_amd64.deb"
    source_path="${SOURCE_ROOT}/${lowercase_name}"

    download-verified "${!url_variable}" "${!sha256_variable}" "${download_path}"
    package_version="$(dpkg-deb --field "${download_path}" Version)"
    test "${package_version%%-*}" = "${!version_variable}"
    clone-commit "${!repository_variable}" "${!commit_variable}" "${source_path}"
    rm -rf "${SOURCE_ROOT}/${lowercase_name}-deb"
    dpkg-deb --extract "${download_path}" "${SOURCE_ROOT}/${lowercase_name}-deb"
done

mkdir -p "${OUTPUT_ROOT}/QSVEncC" "${OUTPUT_ROOT}/NVEncC" "${OUTPUT_ROOT}/VCEEncC"
install -m 0755 "${SOURCE_ROOT}/qsvencc-deb/usr/bin/qsvencc" "${OUTPUT_ROOT}/QSVEncC/QSVEncC.elf"
install -m 0755 "${SOURCE_ROOT}/nvencc-deb/usr/bin/nvencc" "${OUTPUT_ROOT}/NVEncC/NVEncC.elf"
install -m 0755 "${SOURCE_ROOT}/vceencc-deb/usr/bin/vceencc" "${OUTPUT_ROOT}/VCEEncC/VCEEncC.elf"
copy-license "${SOURCE_ROOT}/qsvencc/license.txt" "${OUTPUT_ROOT}/QSVEncC/License.txt"
copy-license "${SOURCE_ROOT}/nvencc/NVEnc_license.txt" "${OUTPUT_ROOT}/NVEncC/License.txt"
copy-license "${SOURCE_ROOT}/vceencc/VCEEnc_license.txt" "${OUTPUT_ROOT}/VCEEncC/License.txt"

# QSVEncC の静的 dispatcher が host の /usr/lib* を優先しないよう、固定長の置換を適用する。
perl -0pi -e 's#/usr/lib/x86_64-linux-gnu#/bundle/disabled/path/001#g; s#/usr/lib64#/bad/path9#g; s#/usr/lib#/bad/lib#g' "${OUTPUT_ROOT}/QSVEncC/QSVEncC.elf"
if strings "${OUTPUT_ROOT}/QSVEncC/QSVEncC.elf" | grep -Fxq '/usr/lib'; then
    echo 'Failed to disable the QSVEncC system library search path.' >&2
    exit 1
fi
patchelf --force-rpath --set-rpath '$ORIGIN:$ORIGIN/../Library' "${OUTPUT_ROOT}/QSVEncC/QSVEncC.elf"
patchelf --set-rpath '$ORIGIN:$ORIGIN/../FFmpeg:$ORIGIN/../Library' "${OUTPUT_ROOT}/NVEncC/NVEncC.elf"
patchelf --set-rpath '$ORIGIN:$ORIGIN/../FFmpeg:$ORIGIN/../Library' "${OUTPUT_ROOT}/VCEEncC/VCEEncC.elf"

# Intel Media Stack を固定 commit と既存の修正 patch から構築する。
OUTPUT_ROOT="${SOURCE_ROOT}/intel-media-stack" "${SCRIPT_DIR}/build-intel-media-stack.sh" "${SOURCE_ROOT}/intel-media-stack"
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

clone-commit "${PSISIMUX_REPOSITORY}" "${PSISIMUX_COMMIT}" "${SOURCE_ROOT}/psisimux"
make -C "${SOURCE_ROOT}/psisimux" -j"$(nproc)"
mkdir -p "${OUTPUT_ROOT}/psisimux"
install -m 0755 "${SOURCE_ROOT}/psisimux/psisimux" "${OUTPUT_ROOT}/psisimux/psisimux.elf"
copy-license "${SOURCE_ROOT}/psisimux/License.txt" "${OUTPUT_ROOT}/psisimux/License.txt"

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
