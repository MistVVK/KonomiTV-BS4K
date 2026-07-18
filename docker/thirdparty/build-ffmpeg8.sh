#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="${SOURCE_ROOT:-/build/sources}"
OUTPUT_ROOT="${OUTPUT_ROOT:-/opt/thirdparty}"
PREFIX="/opt/ffmpeg8"
SDK_PREFIX="/opt/ffmpeg8-sdk"

set -a
source "${SCRIPT_DIR}/manifest.env"
set +a

clone-commit() {
    local url="$1"
    local commit="$2"
    local destination="$3"
    local ref="$4"

    git init "${destination}"
    git -C "${destination}" remote add origin "${url}"
    git -C "${destination}" fetch --depth=1 origin "${ref}:${ref}"
    git -C "${destination}" checkout --detach "${commit}"
    test "$(git -C "${destination}" rev-parse HEAD)" = "${commit}"
}

verify-ffmpeg-component() {
    local config_h="$1"
    local component="$2"
    local config_files=("${config_h}")
    local components_h
    components_h="$(dirname "${config_h}")/config_components.h"
    if [ -f "${components_h}" ]; then
        config_files+=("${components_h}")
    fi
    grep -Fqx "#define CONFIG_${component} 1" "${config_files[@]}" || {
        echo "Missing playback FFmpeg component CONFIG_${component}." >&2
        exit 1
    }
}

ffmpeg_source="${SOURCE_ROOT}/ffmpeg8"
nvcodec_source="${SOURCE_ROOT}/nv-codec-headers"
amf_source="${SOURCE_ROOT}/amf"

clone-commit "${FFMPEG8_REPOSITORY}" "${FFMPEG8_COMMIT}" "${ffmpeg_source}" "refs/tags/${FFMPEG8_TAG}"
clone-commit "${NVCODEC_HEADERS_REPOSITORY}" "${NVCODEC_HEADERS_COMMIT}" "${nvcodec_source}" "refs/tags/${NVCODEC_HEADERS_TAG}"
clone-commit "${AMF_REPOSITORY}" "${AMF_COMMIT}" "${amf_source}" "refs/tags/${AMF_TAG}"
echo "${AMF_DISPLAY_CAPTURE_C_PATCH_SHA256}  ${SCRIPT_DIR}/patches/amf-1.4.36-display-capture-c.patch" | \
    sha256sum --check --strict
patch -d "${amf_source}" -p1 < "${SCRIPT_DIR}/patches/amf-1.4.36-display-capture-c.patch"

# CUVID/NVDEC で実際に組み込む header も固定 commit の checksum で監査する。
# 現在の 12.1.14.0 では 2 つの CUVID header の notice が nvEncodeAPI.h と同一であることも検証し、
# CUDA/loader header のみが共通の別 notice を持つことを明示的に固定する。
# notice が将来変更された場合にライセンス登録なしでビルドを進めない。
echo "${NVCODEC_HEADERS_LICENSE_SHA256}  ${nvcodec_source}/include/ffnvcodec/nvEncodeAPI.h" | \
    sha256sum --check --strict
echo "${NVCODEC_HEADERS_CUDA_SHA256}  ${nvcodec_source}/include/ffnvcodec/dynlink_cuda.h" | \
    sha256sum --check --strict
echo "${NVCODEC_HEADERS_CUVIDDEC_SHA256}  ${nvcodec_source}/include/ffnvcodec/dynlink_cuviddec.h" | \
    sha256sum --check --strict
echo "${NVCODEC_HEADERS_LOADER_SHA256}  ${nvcodec_source}/include/ffnvcodec/dynlink_loader.h" | \
    sha256sum --check --strict
echo "${NVCODEC_HEADERS_NVCUVID_SHA256}  ${nvcodec_source}/include/ffnvcodec/dynlink_nvcuvid.h" | \
    sha256sum --check --strict
cmp \
    <(sed -n '1,/^ \*\/$/p' "${nvcodec_source}/include/ffnvcodec/nvEncodeAPI.h") \
    <(sed -n '1,/^ \*\/$/p' "${nvcodec_source}/include/ffnvcodec/dynlink_cuviddec.h")
cmp \
    <(sed -n '1,/^ \*\/$/p' "${nvcodec_source}/include/ffnvcodec/nvEncodeAPI.h") \
    <(sed -n '1,/^ \*\/$/p' "${nvcodec_source}/include/ffnvcodec/dynlink_nvcuvid.h")
cmp \
    <(sed -n '1,/^ \*\/$/p' "${nvcodec_source}/include/ffnvcodec/dynlink_cuda.h") \
    <(sed -n '1,/^ \*\/$/p' "${nvcodec_source}/include/ffnvcodec/dynlink_loader.h")
if cmp -s \
    <(sed -n '1,/^ \*\/$/p' "${nvcodec_source}/include/ffnvcodec/nvEncodeAPI.h") \
    <(sed -n '1,/^ \*\/$/p' "${nvcodec_source}/include/ffnvcodec/dynlink_cuda.h"); then
    echo 'Expected the audited CUDA/loader notice to be distinct from nvEncodeAPI.h.' >&2
    exit 1
fi

make -C "${nvcodec_source}" PREFIX="${SDK_PREFIX}" install
mkdir -p "${SDK_PREFIX}/include/AMF"
cp -a "${amf_source}/amf/public/include/." "${SDK_PREFIX}/include/AMF/"

export PKG_CONFIG_PATH="${SDK_PREFIX}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
export CFLAGS="-I${SDK_PREFIX}/include"

pushd "${ffmpeg_source}"
./configure \
    --prefix="${PREFIX}" \
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
    --enable-muxer=wav \
    --enable-encoder=pcm_s16le \
    --enable-filter=aresample \
    --enable-filter=asetpts \
    --enable-gnutls \
    --enable-libaom \
    --enable-libass \
    --enable-libbluray \
    --enable-libdrm \
    --enable-libfontconfig \
    --enable-libfreetype \
    --enable-libfribidi \
    --enable-libgsm \
    --enable-libmp3lame \
    --enable-libmysofa \
    --enable-libopenjpeg \
    --enable-libopenmpt \
    --enable-libopus \
    --enable-librabbitmq \
    --enable-librubberband \
    --enable-libshine \
    --enable-libsnappy \
    --enable-libsoxr \
    --enable-libspeex \
    --enable-libsrt \
    --enable-libssh \
    --enable-libtheora \
    --enable-libtwolame \
    --enable-vaapi \
    --enable-filter=deinterlace_vaapi \
    --enable-vdpau \
    --enable-libvidstab \
    --enable-libvorbis \
    --enable-libvpl \
    --enable-libvpx \
    --enable-libwebp \
    --enable-libx264 \
    --enable-libx265 \
    --enable-libxml2 \
    --enable-libxvid \
    --enable-libzimg \
    --enable-libzmq \
    --enable-libzvbi \
    --enable-bzlib \
    --enable-lzma \
    --enable-zlib \
    --enable-opencl \
    --enable-cuda-llvm \
    --enable-amf \
    --enable-ffnvcodec \
    --enable-cuvid \
    --enable-nvdec \
    --enable-nvenc \
    --extra-cflags="-I${SDK_PREFIX}/include" \
    --extra-ldflags='-Wl,-rpath,$ORIGIN'
for component in WAV_MUXER PCM_S16LE_ENCODER ARESAMPLE_FILTER ASETPTS_FILTER; do
    verify-ffmpeg-component config.h "${component}"
done
make -j"$(nproc)"
make install
popd

ffmpeg8_output="${OUTPUT_ROOT}/FFmpeg8"
mkdir -p "${ffmpeg8_output}"
install -m 0755 "${PREFIX}/bin/ffmpeg" "${ffmpeg8_output}/ffmpeg8.elf"
install -m 0755 "${PREFIX}/bin/ffprobe" "${ffmpeg8_output}/ffprobe8.elf"
install -m 0755 "${SCRIPT_DIR}/ffmpeg8-amd.sh" "${ffmpeg8_output}/ffmpeg8-amd.sh"
cp -a "${PREFIX}"/lib/libav*.so* "${PREFIX}"/lib/libsw*.so* "${ffmpeg8_output}/"
install -m 0644 "${ffmpeg_source}/LICENSE.md" "${ffmpeg8_output}/License.txt"
install -m 0644 "${ffmpeg_source}/COPYING.GPLv3" "${ffmpeg8_output}/COPYING.GPLv3"

find "${ffmpeg8_output}" -type f -name '*.so*' -exec patchelf --set-rpath '$ORIGIN' {} +
patchelf --set-rpath '$ORIGIN' "${ffmpeg8_output}/ffmpeg8.elf"
patchelf --set-rpath '$ORIGIN' "${ffmpeg8_output}/ffprobe8.elf"
