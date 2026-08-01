#!/bin/bash
set -euo pipefail

# FFmpeg 8 の QSV 経路が依存する Intel Media Stack
# (gmmlib / libva / media-driver / MediaSDK runtime / oneVPL GPU runtime) を Ubuntu 22.04 の Docker builder 上でビルドし、
# thirdparty/Library/ 以下へそのままコピーできる形へ整えるスクリプト
## Intel Media Stack の構成ライブラリ (OpenCL ランタイムを除く) をすべて自己完結型でビルドすることで、
## 同梱 FFmpeg 8 がシステム側の iHD_drv_video.so や libmfx 実装に依存しないようにし、
## どのような OS 環境・CPU 世代でも QSV 経路を安定的に動作させ続けることが狙い

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_DIR="${SCRIPT_DIR}/patches"
OUTPUT_ROOT="${1:-${REPO_ROOT}/intel-media-stack}"

: "${INTEL_GMMLIB_COMMIT:?INTEL_GMMLIB_COMMIT is required}"
: "${INTEL_LIBVA_COMMIT:?INTEL_LIBVA_COMMIT is required}"
: "${INTEL_MEDIA_DRIVER_COMMIT:?INTEL_MEDIA_DRIVER_COMMIT is required}"
: "${INTEL_MEDIASDK_COMMIT:?INTEL_MEDIASDK_COMMIT is required}"
: "${INTEL_ONEVPL_GPU_COMMIT:?INTEL_ONEVPL_GPU_COMMIT is required}"
: "${INTEL_LIBVA_STANDALONE_PATCH_SHA256:?INTEL_LIBVA_STANDALONE_PATCH_SHA256 is required}"
: "${INTEL_MEDIA_DRIVER_VPP_DEINTERLACE_CRASH_FIX_PATCH_SHA256:?INTEL_MEDIA_DRIVER_VPP_DEINTERLACE_CRASH_FIX_PATCH_SHA256 is required}"
: "${INTEL_ONEVPL_GPU_RT_VPP_DEINTERLACE_HANG_FIX_PATCH_SHA256:?INTEL_ONEVPL_GPU_RT_VPP_DEINTERLACE_HANG_FIX_PATCH_SHA256 is required}"

case "${NONFREE:-}" in
  true)
    MEDIA_DRIVER_PROFILE='full-feature'
    MEDIA_DRIVER_NONFREE_KERNELS='ON'
    ;;
  false)
    MEDIA_DRIVER_PROFILE='free-kernel'
    MEDIA_DRIVER_NONFREE_KERNELS='OFF'
    ;;
  *)
    echo "NONFREE must be exactly 'true' or 'false'. actual: ${NONFREE:-<unset>}" >&2
    exit 2
    ;;
esac

SRC_ROOT="${OUTPUT_ROOT}/src"
BUILD_ROOT="${OUTPUT_ROOT}/build-root"
BUILD_DIR="${BUILD_ROOT}/work"
PREFIX_DIR="${BUILD_DIR}/prefix"
FREE_PREFIX_DIR="${BUILD_DIR}/prefix-free"
ARTIFACT_DIR="${OUTPUT_ROOT}/artifact"

HOST_UID_VALUE="${HOST_UID:-0}"
HOST_GID_VALUE="${HOST_GID:-0}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git patch perl pkg-config build-essential ninja-build cmake meson \
  libdrm-dev libx11-dev libxext-dev libxfixes-dev libxrandr-dev libxrender-dev libxinerama-dev libxcursor-dev libxi-dev \
  libx11-xcb-dev libxcb-dri3-dev libxcb-present-dev \
  wayland-protocols libwayland-dev libwayland-egl-backend-dev libffi-dev libglib2.0-dev libpciaccess-dev libudev-dev libtbb-dev \
  patchelf

mkdir -p "${SRC_ROOT}" "${BUILD_DIR}" "${PREFIX_DIR}" "${FREE_PREFIX_DIR}" \
         "${ARTIFACT_DIR}/Library/dri" "${ARTIFACT_DIR}/Library/libmfx-gen"

export PKG_CONFIG_PATH="${PREFIX_DIR}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
export CMAKE_PREFIX_PATH="${PREFIX_DIR}"
export CMAKE_C_COMPILER_LAUNCHER=ccache
export CMAKE_CXX_COMPILER_LAUNCHER=ccache

apply-git-patch() {
  local source_dir="$1"
  local patch_file="$2"

  if git -C "${source_dir}" apply --check "${patch_file}" >/dev/null 2>&1; then
    git -C "${source_dir}" apply "${patch_file}"
    return 0
  fi

  if git -C "${source_dir}" apply --reverse --check "${patch_file}" >/dev/null 2>&1; then
    echo "Patch already applied. patch: ${patch_file}"
    return 0
  fi

  echo "Failed to apply patch. source: ${source_dir}, patch: ${patch_file}" >&2
  return 1
}

clone-commit() {
  local url="$1"
  local commit="$2"
  local destination="$3"

  if [ ! -d "${destination}/.git" ]; then
    git init "${destination}"
    git -C "${destination}" remote add origin "${url}"
    git -C "${destination}" fetch --depth=1 origin "${commit}"
    git -C "${destination}" checkout --detach FETCH_HEAD
  fi

  test "$(git -C "${destination}" rev-parse HEAD)" = "${commit}"
}

clone-commit https://github.com/intel/gmmlib.git "${INTEL_GMMLIB_COMMIT}" "${SRC_ROOT}/gmmlib"
clone-commit https://github.com/intel/libva.git "${INTEL_LIBVA_COMMIT}" "${SRC_ROOT}/libva"
clone-commit https://github.com/intel/media-driver.git "${INTEL_MEDIA_DRIVER_COMMIT}" "${SRC_ROOT}/media-driver"
clone-commit https://github.com/Intel-Media-SDK/MediaSDK.git "${INTEL_MEDIASDK_COMMIT}" "${SRC_ROOT}/MediaSDK"
clone-commit https://github.com/intel/vpl-gpu-rt.git "${INTEL_ONEVPL_GPU_COMMIT}" "${SRC_ROOT}/vpl-gpu-rt"

echo "${INTEL_LIBVA_STANDALONE_PATCH_SHA256}  ${PATCH_DIR}/intel-libva-standalone.patch" | \
  sha256sum --check --strict
echo "${INTEL_MEDIA_DRIVER_VPP_DEINTERLACE_CRASH_FIX_PATCH_SHA256}  ${PATCH_DIR}/intel-media-driver-vpp-deinterlace-crash-fix.patch" | \
  sha256sum --check --strict
echo "${INTEL_ONEVPL_GPU_RT_VPP_DEINTERLACE_HANG_FIX_PATCH_SHA256}  ${PATCH_DIR}/intel-onevpl-gpu-rt-vpp-deinterlace-hang-fix.patch" | \
  sha256sum --check --strict
apply-git-patch "${SRC_ROOT}/libva" "${PATCH_DIR}/intel-libva-standalone.patch"
apply-git-patch "${SRC_ROOT}/media-driver" "${PATCH_DIR}/intel-media-driver-vpp-deinterlace-crash-fix.patch"
apply-git-patch "${SRC_ROOT}/vpl-gpu-rt" "${PATCH_DIR}/intel-onevpl-gpu-rt-vpp-deinterlace-hang-fix.patch"

# gmmlib
cmake -S "${SRC_ROOT}/gmmlib" -B "${BUILD_DIR}/gmmlib" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="${PREFIX_DIR}" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DRUN_TEST_SUITE=OFF
ninja -C "${BUILD_DIR}/gmmlib" -j"$(nproc)"
ninja -C "${BUILD_DIR}/gmmlib" install

# libva
meson setup "${BUILD_DIR}/libva" "${SRC_ROOT}/libva" \
  --buildtype=release \
  --prefix="${PREFIX_DIR}" \
  --sysconfdir='.' \
  --libdir=lib \
  -Ddriverdir=dri \
  -Dwith_x11=yes \
  -Dwith_wayland=no \
  -Dwith_glx=no \
  -Ddisable_drm=false \
  -Denable_docs=false
ninja -C "${BUILD_DIR}/libva" -j"$(nproc)"
ninja -C "${BUILD_DIR}/libva" install

# media-driver (実配布する iHD driver)
# NONFREE=true では Full Feature Build、false では Free Kernel Build を構築する。
MEDIA_DRIVER_BUILD_DIR="${BUILD_DIR}/media-driver-${MEDIA_DRIVER_PROFILE}"
cmake -S "${SRC_ROOT}/media-driver" -B "${MEDIA_DRIVER_BUILD_DIR}" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="${PREFIX_DIR}" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DINSTALL_DRIVER_SYSCONF=OFF \
  -DENABLE_KERNELS=ON \
  -DENABLE_NONFREE_KERNELS="${MEDIA_DRIVER_NONFREE_KERNELS}" \
  -DENABLE_PRODUCTION_KMD=ON \
  -DBUILD_CMRTLIB=OFF \
  -DARCH=64 \
  -DLIBVA_DRIVERS_PATH="${PREFIX_DIR}/lib/dri" \
  -DCMAKE_PREFIX_PATH="${PREFIX_DIR}" \
  -DBS_DIR_GMMLIB="${SRC_ROOT}/gmmlib"
grep -Fx 'ENABLE_KERNELS:BOOL=ON' "${MEDIA_DRIVER_BUILD_DIR}/CMakeCache.txt"
grep -Fx "ENABLE_NONFREE_KERNELS:BOOL=${MEDIA_DRIVER_NONFREE_KERNELS}" "${MEDIA_DRIVER_BUILD_DIR}/CMakeCache.txt"
grep -Fx 'BUILD_CMRTLIB:BOOL=OFF' "${MEDIA_DRIVER_BUILD_DIR}/CMakeCache.txt"
for definition in _MPEG2_DECODE_SUPPORTED _AVC_DECODE_SUPPORTED _HEVC_DECODE_SUPPORTED; do
  grep -Fq -- "-D${definition}" "${MEDIA_DRIVER_BUILD_DIR}/build.ninja"
done
if [ "${NONFREE}" = 'false' ]; then
  grep -Fq -- '-D_FULL_OPEN_SOURCE' "${MEDIA_DRIVER_BUILD_DIR}/build.ninja"
elif grep -Fq -- '-D_FULL_OPEN_SOURCE' "${MEDIA_DRIVER_BUILD_DIR}/build.ninja"; then
  echo 'Full Feature media-driver unexpectedly defines _FULL_OPEN_SOURCE.' >&2
  exit 1
fi
ninja -C "${MEDIA_DRIVER_BUILD_DIR}" -j"$(nproc)"
ninja -C "${MEDIA_DRIVER_BUILD_DIR}" install

# media-driver (CMRT 用のフリーカーネル版)
MEDIA_DRIVER_CMRT_BUILD_DIR="${BUILD_DIR}/media-driver-cmrt-free"
cmake -S "${SRC_ROOT}/media-driver" -B "${MEDIA_DRIVER_CMRT_BUILD_DIR}" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="${FREE_PREFIX_DIR}" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DINSTALL_DRIVER_SYSCONF=OFF \
  -DENABLE_KERNELS=ON \
  -DENABLE_NONFREE_KERNELS=OFF \
  -DENABLE_PRODUCTION_KMD=ON \
  -DBUILD_CMRTLIB=ON \
  -DARCH=64 \
  -DLIBVA_DRIVERS_PATH="${FREE_PREFIX_DIR}/lib/dri" \
  -DCMAKE_PREFIX_PATH="${PREFIX_DIR}" \
  -DBS_DIR_GMMLIB="${SRC_ROOT}/gmmlib"
# フリー構成から必要なのは CMRT runtime だけであり、iHD driver 本体は上の実配布構成を採用する。
# all/install target は media-driver 全体をもう一度構築するため、CMRT target のみに限定する。
grep -Fx 'ENABLE_NONFREE_KERNELS:BOOL=OFF' "${MEDIA_DRIVER_CMRT_BUILD_DIR}/CMakeCache.txt"
grep -Fx 'ENABLE_KERNELS:BOOL=ON' "${MEDIA_DRIVER_CMRT_BUILD_DIR}/CMakeCache.txt"
grep -Fx 'BUILD_CMRTLIB:BOOL=ON' "${MEDIA_DRIVER_CMRT_BUILD_DIR}/CMakeCache.txt"
ninja -C "${MEDIA_DRIVER_CMRT_BUILD_DIR}" -j"$(nproc)" igfxcmrt

# 旧世代 GPU 向けの後方互換性を維持するため、MediaSDK 系の実ランタイムのみをビルドする
# FFmpeg 8 はランタイムコンテナの oneVPL dispatcher を使うため、旧 MediaSDK dispatcher の libmfx.so.1 は同梱しない
cmake -S "${SRC_ROOT}/MediaSDK" -B "${BUILD_DIR}/MediaSDK" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="${PREFIX_DIR}" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DENABLE_OPENCL=OFF \
  -DENABLE_X11_DRI3=ON \
  -DENABLE_WAYLAND=ON \
  -DENABLE_TEXTLOG=ON \
  -DENABLE_STAT=ON \
  -DBUILD_RUNTIME=ON \
  -DBUILD_DISPATCHER=OFF \
  -DBUILD_SAMPLES=OFF \
  -DBUILD_TUTORIALS=OFF \
  -DBUILD_TOOLS=OFF \
  -DBUILD_TESTS=OFF
ninja -C "${BUILD_DIR}/MediaSDK" -j"$(nproc)"
ninja -C "${BUILD_DIR}/MediaSDK" install

# oneVPL GPU ランタイム
cmake -S "${SRC_ROOT}/vpl-gpu-rt" -B "${BUILD_DIR}/vpl-gpu-rt" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="${PREFIX_DIR}" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DCMAKE_PREFIX_PATH="${PREFIX_DIR}" \
  -DENABLE_OPENCL=OFF \
  -DBUILD_RUNTIME=ON \
  -DMFX_ENABLE_ENCTOOLS=ON \
  -DMFX_ENABLE_AENC=ON \
  -DBUILD_TESTS=OFF \
  -DBUILD_TOOLS=OFF
ninja -C "${BUILD_DIR}/vpl-gpu-rt" -j"$(nproc)"
ninja -C "${BUILD_DIR}/vpl-gpu-rt" install

# 成果物を同梱用ディレクトリへ集約する
cp -df "${PREFIX_DIR}/lib/libigdgmm.so"* "${ARTIFACT_DIR}/Library/"
cp -df "${PREFIX_DIR}/lib/libva.so"* "${ARTIFACT_DIR}/Library/"
cp -df "${PREFIX_DIR}/lib/libva-drm.so"* "${ARTIFACT_DIR}/Library/"
cp -df "${PREFIX_DIR}/lib/libmfx-gen.so"* "${ARTIFACT_DIR}/Library/"
cp -df "${PREFIX_DIR}/lib/libmfxhw64.so"* "${ARTIFACT_DIR}/Library/"
cp -df "${MEDIA_DRIVER_CMRT_BUILD_DIR}/cmrtlib/linux/libigfxcmrt.so"* "${ARTIFACT_DIR}/Library/"
cp -f "${PREFIX_DIR}/lib/dri/iHD_drv_video.so" "${ARTIFACT_DIR}/Library/dri/"
{
  printf 'source_commit=%s\n' "${INTEL_MEDIA_DRIVER_COMMIT}"
  printf 'nonfree=%s\n' "${NONFREE}"
  grep -E '^(ENABLE_KERNELS|ENABLE_NONFREE_KERNELS|BUILD_CMRTLIB):BOOL=' \
    "${MEDIA_DRIVER_BUILD_DIR}/CMakeCache.txt"
  grep -E '^(ENABLE_KERNELS|ENABLE_NONFREE_KERNELS|BUILD_CMRTLIB):BOOL=' \
    "${MEDIA_DRIVER_CMRT_BUILD_DIR}/CMakeCache.txt" | sed 's/^/cmrt_/'
  printf 'patch_intel_libva_standalone_sha256=%s\n' "${INTEL_LIBVA_STANDALONE_PATCH_SHA256}"
  printf 'patch_intel_media_driver_vpp_deinterlace_crash_fix_sha256=%s\n' \
    "${INTEL_MEDIA_DRIVER_VPP_DEINTERLACE_CRASH_FIX_PATCH_SHA256}"
  printf 'patch_intel_onevpl_gpu_rt_vpp_deinterlace_hang_fix_sha256=%s\n' \
    "${INTEL_ONEVPL_GPU_RT_VPP_DEINTERLACE_HANG_FIX_PATCH_SHA256}"
  if [ "${NONFREE}" = 'false' ]; then
    printf 'compile_definition=_FULL_OPEN_SOURCE\n'
  fi
  printf 'compile_definition=_MPEG2_DECODE_SUPPORTED\n'
  printf 'compile_definition=_AVC_DECODE_SUPPORTED\n'
  printf 'compile_definition=_HEVC_DECODE_SUPPORTED\n'
} > "${ARTIFACT_DIR}/Library/Intel-Media-Driver-Build-Configuration.txt"
if [ -d "${PREFIX_DIR}/lib/libmfx-gen" ]; then
  cp -af "${PREFIX_DIR}/lib/libmfx-gen/." "${ARTIFACT_DIR}/Library/libmfx-gen/"
fi
if compgen -G "${PREFIX_DIR}/lib/libva-x11.so*" > /dev/null; then
  cp -df "${PREFIX_DIR}/lib/libva-x11.so"* "${ARTIFACT_DIR}/Library/"
fi
if compgen -G "${PREFIX_DIR}/lib/libva-wayland.so*" > /dev/null; then
  cp -df "${PREFIX_DIR}/lib/libva-wayland.so"* "${ARTIFACT_DIR}/Library/"
fi

# ファイルサイズ削減のため、Intel 系ライブラリのデバッグシンボルを削除する
strip --strip-debug "${ARTIFACT_DIR}/Library/libigdgmm.so"* || true
strip --strip-debug "${ARTIFACT_DIR}/Library/dri/iHD_drv_video.so" || true
strip --strip-debug "${ARTIFACT_DIR}/Library/libva.so"* "${ARTIFACT_DIR}/Library/libva-drm.so"* || true
if compgen -G "${ARTIFACT_DIR}/Library/libva-x11.so*" > /dev/null; then
  strip --strip-debug "${ARTIFACT_DIR}/Library/libva-x11.so"* || true
fi
if compgen -G "${ARTIFACT_DIR}/Library/libva-wayland.so*" > /dev/null; then
  strip --strip-debug "${ARTIFACT_DIR}/Library/libva-wayland.so"* || true
fi
strip --strip-debug "${ARTIFACT_DIR}/Library/libmfxhw64.so"* \
                     "${ARTIFACT_DIR}/Library/libmfx-gen.so"* "${ARTIFACT_DIR}/Library/libigfxcmrt.so"* || true
find "${ARTIFACT_DIR}/Library/libmfx-gen" -type f -name '*.so*' -exec strip --strip-debug {} + || true

# 実行時ライブラリ探索パスはビルド時の絶対パスを残さず、サードパーティーライブラリの配置先を基準とした相対パスだけにそろえる
find "${ARTIFACT_DIR}/Library" -maxdepth 1 -type f -name '*.so*' | while read -r file; do
  patchelf --set-rpath '$ORIGIN' "${file}"
  chmod +x "${file}"
done
find "${ARTIFACT_DIR}/Library/libmfx-gen" -type f -name '*.so*' | while read -r file; do
  patchelf --set-rpath '$ORIGIN:$ORIGIN/..' "${file}"
  chmod +x "${file}"
done
patchelf --set-rpath '$ORIGIN:$ORIGIN/..' "${ARTIFACT_DIR}/Library/dri/iHD_drv_video.so"
chmod +x "${ARTIFACT_DIR}/Library/dri/iHD_drv_video.so"

chown -R "${HOST_UID_VALUE}:${HOST_GID_VALUE}" "${OUTPUT_ROOT}" || true
