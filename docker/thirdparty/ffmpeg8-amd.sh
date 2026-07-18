#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# AMD の VAAPI ドライバーをこの FFmpeg プロセスだけに固定し、同じイメージに同梱した
# Intel iHD ドライバーを利用するプロセスの自動検出へ影響を与えないようにする。
export LIBVA_DRIVER_NAME=radeonsi
export LIBVA_DRIVERS_PATH=/opt/amdgpu/lib/x86_64-linux-gnu/dri

# 複数 GPU 環境では renderD128 が AMD とは限らないため、VAAPI デバイスは固定せず
# 呼び出し元から -init_hw_device などで対象の render node を明示してもらう。
exec "${SCRIPT_DIR}/ffmpeg8.elf" "$@"
