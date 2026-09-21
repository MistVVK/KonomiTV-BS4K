#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="${SOURCE_ROOT:-/build/sources}"
OUTPUT_ROOT="${OUTPUT_ROOT:-/opt/thirdparty}"
CM_ROOT="${OUTPUT_ROOT}/CMAnalysis"
FFMPEG_SDK_ROOT="/opt/ffmpeg8-sdk"

set -a
source "${SCRIPT_DIR}/manifest.env"
set +a

clone-commit() {
    local repository="$1"
    local commit="$2"
    local destination="$3"
    local ref="${4:-${commit}}"

    git init "${destination}"
    git -C "${destination}" remote add origin "${repository}"
    if [[ "${ref}" == refs/tags/* ]]; then
        git -C "${destination}" fetch --depth=1 origin "${ref}:${ref}"
        git -C "${destination}" checkout --detach "${commit}"
    else
        git -C "${destination}" fetch --depth=1 origin "${commit}"
        git -C "${destination}" checkout --detach FETCH_HEAD
    fi
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
        echo "Missing CM FFmpeg component CONFIG_${component}." >&2
        exit 1
    }
}

mkdir -p "${SOURCE_ROOT}" "${CM_ROOT}/JL"
test ! -e "${OUTPUT_ROOT}/Amatsukaze"

# FFMS2 is loaded into the GPLv2-or-later AviSynth process. Build a private,
# LGPL-compatible FFmpeg from the same audited FFmpeg 8 revision instead of
# loading the GPLv3 playback build into that process.
analysis_ffmpeg_source="${SOURCE_ROOT}/ffmpeg8-cm-analysis"
analysis_ffmpeg_install="${SOURCE_ROOT}/ffmpeg8-cm-analysis-install"
clone-commit \
    "${FFMPEG8_REPOSITORY}" "${FFMPEG8_COMMIT}" "${analysis_ffmpeg_source}" \
    "refs/tags/${FFMPEG8_TAG}"
test -s "${FFMPEG_SDK_ROOT}/lib/pkgconfig/ffnvcodec.pc"
test -s "${FFMPEG_SDK_ROOT}/include/AMF/core/Platform.h"

analysis_ffmpeg_configure=(
    "--prefix=${analysis_ffmpeg_install}"
    "--cc=ccache gcc"
    "--cxx=ccache g++"
    --disable-autodetect
    --disable-debug
    --disable-doc
    --disable-programs
    --disable-network
    --disable-devices
    --enable-decoders
    --enable-pic
    --enable-shared
    --disable-static
    --enable-demuxer=wav
    --enable-decoder=pcm_s16le
    --enable-bzlib
    --enable-lzma
    --enable-zlib
    --enable-libaom
    --enable-libdrm
    --enable-libvpl
    --enable-vaapi
    --enable-amf
    --enable-ffnvcodec
    --enable-cuvid
    --enable-nvdec
    --enable-hwaccel=h264_vaapi
    --enable-hwaccel=hevc_vaapi
    --enable-hwaccel=av1_vaapi
    --enable-hwaccel=h264_nvdec
    --enable-hwaccel=hevc_nvdec
    --enable-hwaccel=av1_nvdec
    "--extra-cflags=-I${FFMPEG_SDK_ROOT}/include"
    '--extra-ldflags=-Wl,-rpath,$ORIGIN'
)
analysis_ffmpeg_configure_sha256="$(printf '%s\n' "${analysis_ffmpeg_configure[@]}" | sha256sum | cut -d' ' -f1)"
pushd "${analysis_ffmpeg_source}"
PKG_CONFIG_PATH="${FFMPEG_SDK_ROOT}/lib/pkgconfig" CFLAGS="-I${FFMPEG_SDK_ROOT}/include" \
    ./configure "${analysis_ffmpeg_configure[@]}"
grep -Fqx '#define CONFIG_GPL 0' config.h
grep -Fqx '#define CONFIG_VERSION3 0' config.h
grep -Fqx '#define CONFIG_NONFREE 0' config.h
for component in \
    AVCODEC AVFORMAT AVUTIL SWRESAMPLE SWSCALE \
    FILE_PROTOCOL MPEGTS_DEMUXER MOV_DEMUXER MATROSKA_DEMUXER OGG_DEMUXER WAV_DEMUXER \
    MPEG1VIDEO_DECODER MPEG2VIDEO_DECODER MPEG4_DECODER H264_DECODER HEVC_DECODER \
    AV1_DECODER LIBAOM_AV1_DECODER VP8_DECODER VP9_DECODER THEORA_DECODER PRORES_DECODER FFV1_DECODER \
    DNXHD_DECODER MJPEG_DECODER VC1_DECODER \
    PCM_S16LE_DECODER \
    H264_QSV_DECODER HEVC_QSV_DECODER AV1_QSV_DECODER \
    H264_CUVID_DECODER HEVC_CUVID_DECODER AV1_CUVID_DECODER \
    H264_AMF_DECODER HEVC_AMF_DECODER AV1_AMF_DECODER \
    H264_VAAPI_HWACCEL HEVC_VAAPI_HWACCEL AV1_VAAPI_HWACCEL \
    H264_NVDEC_HWACCEL HEVC_NVDEC_HWACCEL AV1_NVDEC_HWACCEL; do
    verify-ffmpeg-component config.h "${component}"
done
make -j"$(nproc)"
make install
popd

cp -a \
    "${analysis_ffmpeg_install}"/lib/libavcodec.so* \
    "${analysis_ffmpeg_install}"/lib/libavformat.so* \
    "${analysis_ffmpeg_install}"/lib/libavutil.so* \
    "${analysis_ffmpeg_install}"/lib/libswresample.so* \
    "${analysis_ffmpeg_install}"/lib/libswscale.so* \
    "${CM_ROOT}/"
install -m 0644 "${analysis_ffmpeg_source}/COPYING.LGPLv2.1" \
    "${CM_ROOT}/License-FFmpeg-LGPLv2.1.txt"
{
    printf 'source_commit=%s\n' "${FFMPEG8_COMMIT}"
    printf 'license=LGPL-2.1-or-later\n'
    printf 'CONFIG_GPL=0\nCONFIG_VERSION3=0\nCONFIG_NONFREE=0\n'
    printf 'configure_sha256=%s\n' "${analysis_ffmpeg_configure_sha256}"
    printf 'configure_option=%s\n' "${analysis_ffmpeg_configure[@]}"
} > "${CM_ROOT}/FFmpeg-Build-Configuration.txt"

# chapter_exe and logoframe dynamically load a minimal AviSynth+ core.
clone-commit \
    "${AVISYNTHPLUS_REPOSITORY}" "${AVISYNTHPLUS_COMMIT}" "${SOURCE_ROOT}/avisynthplus" \
    "refs/tags/${AVISYNTHPLUS_TAG}"
cmake -S "${SOURCE_ROOT}/avisynthplus" -B "${SOURCE_ROOT}/avisynthplus-build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="${SOURCE_ROOT}/avisynthplus-install" \
    -DBUILD_SHARED_LIBS=ON \
    -DENABLE_PLUGINS=OFF
cmake --build "${SOURCE_ROOT}/avisynthplus-build" --parallel "$(nproc)"
cmake --install "${SOURCE_ROOT}/avisynthplus-build"
AVISYNTH_PKGCONFIG="$(find "${SOURCE_ROOT}/avisynthplus-install" -name avisynth.pc -printf '%h' -quit)"
test -n "${AVISYNTH_PKGCONFIG}"

# Build FFMS2 as an AviSynth-only plugin and indexer against the private FFmpeg.
# The local patch adds explicit VAAPI/CUDA selection while retaining the legacy
# C API and CPU decode path.
ffms2_source="${SOURCE_ROOT}/ffms2"
ffms2_build="${SOURCE_ROOT}/ffms2-build"
ffms2_install="${SOURCE_ROOT}/ffms2-install"
clone-commit "${FFMS2_REPOSITORY}" "${FFMS2_COMMIT}" "${ffms2_source}"
patch --fuzz=0 -d "${ffms2_source}" -p1 < "${SCRIPT_DIR}/patches/ffms2-hardware-decoding.patch"
pushd "${ffms2_source}"
NOCONFIGURE=1 ./autogen.sh
popd
mkdir -p "${ffms2_build}"
pushd "${ffms2_build}"
PKG_CONFIG_PATH="${analysis_ffmpeg_install}/lib/pkgconfig:${AVISYNTH_PKGCONFIG}" \
CXXFLAGS="-O3 -I${SOURCE_ROOT}/avisynthplus-install/include/avisynth" \
LDFLAGS="-L${analysis_ffmpeg_install}/lib -Wl,-rpath-link,${analysis_ffmpeg_install}/lib" \
    "${ffms2_source}/configure" \
        --prefix="${ffms2_install}" \
        --disable-static \
        --enable-shared \
        --enable-avisynth
make -j"$(nproc)"
make install
popd

clone-commit "${CHAPTER_EXE_REPOSITORY}" "${CHAPTER_EXE_COMMIT}" "${SOURCE_ROOT}/chapter_exe"
patch --fuzz=0 -d "${SOURCE_ROOT}/chapter_exe" -p1 < "${SCRIPT_DIR}/patches/chapter-exe-initialize-avisynth.patch"
make -C "${SOURCE_ROOT}/chapter_exe/src" -j"$(nproc)" \
    CFLAGS="-O3 -I${SOURCE_ROOT}/avisynthplus-install/include/avisynth -ffast-math -Wall -Wshadow -Wempty-body -I. -std=gnu99 -fpermissive -fomit-frame-pointer -fno-tree-vectorize" \
    LDLAGS="-ldl -lstdc++ -pthread"

clone-commit "${LOGOFRAME_REPOSITORY}" "${LOGOFRAME_COMMIT}" "${SOURCE_ROOT}/logoframe"
# The pinned upstream source uses CRLF in these patched files.
sed -i 's/\r$//' \
    "${SOURCE_ROOT}/logoframe/src/logoframe.c" \
    "${SOURCE_ROOT}/logoframe/src/logoframe_det.c" \
    "${SOURCE_ROOT}/logoframe/src/logoframe_mul.c"
patch --fuzz=0 -d "${SOURCE_ROOT}/logoframe" -p1 < "${SCRIPT_DIR}/patches/logoframe-error-lifetime.patch"
patch --fuzz=0 -d "${SOURCE_ROOT}/logoframe" -p1 < "${SCRIPT_DIR}/patches/logoframe-parallel-scan.patch"
patch --fuzz=0 -d "${SOURCE_ROOT}/logoframe" -p1 < "${SCRIPT_DIR}/patches/logoframe-native-luma.patch"
patch --fuzz=0 -d "${SOURCE_ROOT}/logoframe" -p1 < "${SCRIPT_DIR}/patches/logoframe-high-bit-rgb-fallback.patch"
make -C "${SOURCE_ROOT}/logoframe/src" -j"$(nproc)" \
    CFLAGS="-O3 -xc++ -I${SOURCE_ROOT}/avisynthplus-install/include/avisynth -ffast-math -Wall -Wshadow -fpermissive -Wempty-body -I. -fomit-frame-pointer -fno-tree-vectorize" \
    LDLAGS="-ldl -pthread -lstdc++"

clone-commit "${JOIN_LOGO_SCP_REPOSITORY}" "${JOIN_LOGO_SCP_COMMIT}" "${SOURCE_ROOT}/join_logo_scp"
make -C "${SOURCE_ROOT}/join_logo_scp/src" -j"$(nproc)"

install -m 0755 "${SOURCE_ROOT}/chapter_exe/src/chapter_exe" "${CM_ROOT}/chapter_exe"
install -m 0755 "${SOURCE_ROOT}/logoframe/src/logoframe" "${CM_ROOT}/logoframe"
install -m 0755 "${SOURCE_ROOT}/join_logo_scp/src/join_logo_scp" "${CM_ROOT}/join_logo_scp"
install -m 0755 "${ffms2_install}/bin/ffmsindex" "${CM_ROOT}/ffmsindex"
ffms2_library="$(find "${ffms2_install}" -type f -name 'libffms2.so.*' -print -quit)"
test -n "${ffms2_library}"
ffms2_library_dir="$(dirname "${ffms2_library}")"
cp -a "${ffms2_library_dir}"/libffms2.so* "${CM_ROOT}/"
install -m 0755 "$(find "${SOURCE_ROOT}/avisynthplus-install" -name libavisynth.so.10 -print -quit)" \
    "${CM_ROOT}/libavisynth.so.10"
ln -s libavisynth.so.10 "${CM_ROOT}/libavisynth.so"
install -m 0644 "${SOURCE_ROOT}/join_logo_scp/JL/JL_標準.txt" "${CM_ROOT}/JL/JL_標準.txt"

install -m 0644 "${SOURCE_ROOT}/avisynthplus/distrib/gpl.txt" "${CM_ROOT}/License-AviSynthPlus-GPLv2.txt"
install -m 0644 "${ffms2_source}/COPYING" "${CM_ROOT}/License-FFMS2-MIT.txt"
install -m 0644 "${SOURCE_ROOT}/chapter_exe/LICENSE" "${CM_ROOT}/License-chapter_exe-GPLv2.txt"
install -m 0644 "${SOURCE_ROOT}/logoframe/LICENSE" "${CM_ROOT}/License-logoframe-GPLv2.txt"
install -m 0644 "${SOURCE_ROOT}/join_logo_scp/LICENSE" "${CM_ROOT}/License-join_logo_scp-GPLv2.txt"

# The server hashes this deterministic file to invalidate stale CM results when
# any native component, command file, or local compatibility patch changes.
# 各 hash は manifest.env の固定値ではなく、実際に使ったファイルから都度算出する
# (事前固定すると patch やコマンド定義の更新ごとに manifest の更新も必要になり、
# 忘れた場合にビルドが止まるため)。
join_logo_scp_command_sha256="$(sha256sum "${SOURCE_ROOT}/join_logo_scp/JL/JL_標準.txt" | cut -d' ' -f1)"
chapter_exe_avisynth_init_patch_sha256="$(sha256sum "${SCRIPT_DIR}/patches/chapter-exe-initialize-avisynth.patch" | cut -d' ' -f1)"
ffms2_hardware_decoding_patch_sha256="$(sha256sum "${SCRIPT_DIR}/patches/ffms2-hardware-decoding.patch" | cut -d' ' -f1)"
logoframe_error_lifetime_patch_sha256="$(sha256sum "${SCRIPT_DIR}/patches/logoframe-error-lifetime.patch" | cut -d' ' -f1)"
logoframe_parallel_scan_patch_sha256="$(sha256sum "${SCRIPT_DIR}/patches/logoframe-parallel-scan.patch" | cut -d' ' -f1)"
logoframe_native_luma_patch_sha256="$(sha256sum "${SCRIPT_DIR}/patches/logoframe-native-luma.patch" | cut -d' ' -f1)"
logoframe_high_bit_rgb_fallback_patch_sha256="$(sha256sum "${SCRIPT_DIR}/patches/logoframe-high-bit-rgb-fallback.patch" | cut -d' ' -f1)"
{
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "components": {\n'
    printf '    "avisynthplus": {"version": "%s", "commit": "%s", "profile": "shared-core-only"},\n' \
        "${AVISYNTHPLUS_TAG#v}" "${AVISYNTHPLUS_COMMIT}"
    printf '    "chapter_exe": {"commit": "%s"},\n' "${CHAPTER_EXE_COMMIT}"
    printf '    "ffmpeg": {"version": "%s", "commit": "%s", "profile": "lgpl-cm-analysis", "configure_sha256": "%s"},\n' \
        "${FFMPEG8_VERSION}" "${FFMPEG8_COMMIT}" "${analysis_ffmpeg_configure_sha256}"
    printf '    "ffms2": {"version": "%s", "commit": "%s", "profile": "avisynth-only-hardware"},\n' \
        "${FFMS2_VERSION}" "${FFMS2_COMMIT}"
    printf '    "join_logo_scp": {"commit": "%s", "command_sha256": "%s"},\n' \
        "${JOIN_LOGO_SCP_COMMIT}" "${join_logo_scp_command_sha256}"
    printf '    "logoframe": {"version": "%s", "commit": "%s"}\n' \
        "${LOGOFRAME_VERSION}" "${LOGOFRAME_COMMIT}"
    printf '  },\n'
    printf '  "patches": {\n'
    printf '    "chapter-exe-initialize-avisynth.patch": "%s",\n' "${chapter_exe_avisynth_init_patch_sha256}"
    printf '    "ffms2-hardware-decoding.patch": "%s",\n' "${ffms2_hardware_decoding_patch_sha256}"
    printf '    "logoframe-error-lifetime.patch": "%s",\n' "${logoframe_error_lifetime_patch_sha256}"
    printf '    "logoframe-parallel-scan.patch": "%s",\n' "${logoframe_parallel_scan_patch_sha256}"
    printf '    "logoframe-native-luma.patch": "%s",\n' "${logoframe_native_luma_patch_sha256}"
    printf '    "logoframe-high-bit-rgb-fallback.patch": "%s"\n' \
        "${logoframe_high_bit_rgb_fallback_patch_sha256}"
    printf '  }\n'
    printf '}\n'
} > "${CM_ROOT}/Runtime-Manifest.json"

find "${CM_ROOT}" -type f -name '*.so*' -exec patchelf --set-rpath '$ORIGIN' {} +
for executable in chapter_exe ffmsindex logoframe join_logo_scp; do
    patchelf --set-rpath '$ORIGIN' "${CM_ROOT}/${executable}"
done
test ! -e "${CM_ROOT}/logoframe.ini"
