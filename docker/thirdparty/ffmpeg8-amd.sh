#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIBRARY_DIR="$(cd "${SCRIPT_DIR}/../Library" && pwd)"

# Ubuntu 22.04 側の libva と driver のビルド時 ABI がずれても挙動が変わらないよう、
# vendor-neutral な同梱 libva を Intel/AMD で共用する。
export LD_LIBRARY_PATH="${LIBRARY_DIR}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

# AMD の VAAPI ドライバーをこの FFmpeg プロセスだけに固定し、同じイメージに同梱した
# Intel iHD ドライバーを利用するプロセスの自動検出へ影響を与えないようにする。
export LIBVA_DRIVER_NAME=radeonsi
if [ -e /opt/amdgpu/lib/x86_64-linux-gnu/dri/radeonsi_drv_video.so ]; then
    export LIBVA_DRIVERS_PATH=/opt/amdgpu/lib/x86_64-linux-gnu/dri
else
    # INSTALL_AMD=false の再配布可能構成では Ubuntu の Mesa VAAPI driver を使う。
    export LIBVA_DRIVERS_PATH=/usr/lib/x86_64-linux-gnu/dri
fi

# 複数 GPU 環境では renderD128 が AMD とは限らないため、VAAPI デバイスは固定せず
# 呼び出し元から -init_hw_device などで対象の render node を明示してもらう。
exec "${SCRIPT_DIR}/ffmpeg8.elf" "$@"
