#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
THIRDPARTY_ROOT="${1:-/opt/thirdparty}"

set -a
source "${SCRIPT_DIR}/manifest.env"
set +a

required_files=(
    Akebi/akebi-https-server.elf
    FFmpeg/ffmpeg.elf
    FFmpeg/ffprobe.elf
    NVEncC/NVEncC.elf
    Python/bin/python
    QSVEncC/QSVEncC.elf
    VCEEncC/VCEEncC.elf
    psisiarc/psisiarc.elf
    psisimux/psisimux.elf
    tsreadex/tsreadex.elf
    Library/dri/iHD_drv_video.so
    Library/libmfx-gen.so.1.2
    Library/libmfxhw64.so.1
    Library/libva.so.2
    Library/libva-x11.so.2
)

for relative_path in "${required_files[@]}"; do
    echo "Verifying file: ${relative_path}"
    test -s "${THIRDPARTY_ROOT}/${relative_path}" || { echo "Missing file: ${relative_path}" >&2; exit 1; }
    file "${THIRDPARTY_ROOT}/${relative_path}"
    file "${THIRDPARTY_ROOT}/${relative_path}" | grep -Eq 'ELF 64-bit|Python script|symbolic link'
done

echo 'Verifying component versions.'
echo "Expected FFmpeg: ${FFMPEG_VERSION}"
ffmpeg_version="$(${THIRDPARTY_ROOT}/FFmpeg/ffmpeg.elf -version 2>&1)" || { echo "${ffmpeg_version}" >&2; exit 1; }
echo "${ffmpeg_version}"
echo "${ffmpeg_version}" | grep -F "ffmpeg version n${FFMPEG_VERSION}"
ffprobe_version="$(${THIRDPARTY_ROOT}/FFmpeg/ffprobe.elf -version 2>&1)" || { echo "${ffprobe_version}" >&2; exit 1; }
echo "${ffprobe_version}"
echo "${ffprobe_version}" | grep -F "ffprobe version n${FFMPEG_VERSION}"
echo "Expected Python: ${PYTHON_VERSION}"
"${THIRDPARTY_ROOT}/Python/bin/python" --version | grep -F "Python ${PYTHON_VERSION}"
echo "Expected Poetry: ${POETRY_VERSION}"
"${THIRDPARTY_ROOT}/Python/bin/python" -m poetry --version | grep -F "Poetry (version ${POETRY_VERSION})"
echo "Expected QSVEncC: ${QSVENCC_VERSION}"
"${THIRDPARTY_ROOT}/QSVEncC/QSVEncC.elf" --version | grep -F "${QSVENCC_VERSION}"
echo "Expected NVEncC: ${NVENCC_VERSION}"
if ! nvencc_version="$("${THIRDPARTY_ROOT}/NVEncC/NVEncC.elf" --version 2>&1)"; then
    echo "${nvencc_version}"
    echo "${nvencc_version}" | grep -F 'libcuda.so.1' > /dev/null
    echo 'NVEncC --version is deferred to the runtime container with the host NVIDIA driver.'
else
    echo "${nvencc_version}" | grep -F "${NVENCC_VERSION}"
fi
echo "Expected VCEEncC: ${VCEENCC_VERSION}"
if ! vceencc_version="$("${THIRDPARTY_ROOT}/VCEEncC/VCEEncC.elf" --version 2>&1)"; then
    echo "${vceencc_version}"
    echo "${vceencc_version}" | grep -E 'libamfrt|libOpenCL' > /dev/null
    echo 'VCEEncC --version is deferred to the runtime container with the AMD runtime.'
else
    echo "${vceencc_version}" | grep -F "${VCEENCC_VERSION}"
fi

for executable in \
    FFmpeg/ffmpeg.elf \
    FFmpeg/ffprobe.elf \
    QSVEncC/QSVEncC.elf \
    NVEncC/NVEncC.elf \
    VCEEncC/VCEEncC.elf; do
    echo "Verifying dynamic dependencies: ${executable}"
    missing_dependencies="$(ldd "${THIRDPARTY_ROOT}/${executable}" | awk '/not found/ { print $1 }')"
    case "${executable}" in
        NVEncC/*)
            missing_dependencies="$(echo "${missing_dependencies}" | grep -Ev '^(libcuda\.so\.1|libnvidia-encode\.so\.1)?$' || true)"
            ;;
        VCEEncC/*)
            missing_dependencies="$(echo "${missing_dependencies}" | grep -Ev '^(libamfrt64\.so\.1|libOpenCL\.so\.1)?$' || true)"
            ;;
    esac
    if [ -n "${missing_dependencies}" ]; then
        ldd "${THIRDPARTY_ROOT}/${executable}" >&2
        exit 1
    fi
done

test "$(patchelf --print-rpath "${THIRDPARTY_ROOT}/QSVEncC/QSVEncC.elf")" = '$ORIGIN:$ORIGIN/../Library'
test ! -d "${THIRDPARTY_ROOT}/Library/lib"
test ! -d "${THIRDPARTY_ROOT}/Library/usr"
