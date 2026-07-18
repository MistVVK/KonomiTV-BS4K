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
    FFmpeg8/ffmpeg8-amd.sh
    FFmpeg8/ffmpeg8.elf
    FFmpeg8/ffprobe8.elf
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
    file "${THIRDPARTY_ROOT}/${relative_path}" | grep -Eq 'ELF 64-bit|Python script|shell script|symbolic link'
done

echo 'Verifying component versions.'
echo "Expected FFmpeg: ${FFMPEG_VERSION}"
ffmpeg_version="$(${THIRDPARTY_ROOT}/FFmpeg/ffmpeg.elf -version 2>&1)" || { echo "${ffmpeg_version}" >&2; exit 1; }
echo "${ffmpeg_version}"
echo "${ffmpeg_version}" | grep -F "ffmpeg version n${FFMPEG_VERSION}"
ffprobe_version="$(${THIRDPARTY_ROOT}/FFmpeg/ffprobe.elf -version 2>&1)" || { echo "${ffprobe_version}" >&2; exit 1; }
echo "${ffprobe_version}"
echo "${ffprobe_version}" | grep -F "ffprobe version n${FFMPEG_VERSION}"
echo "Expected FFmpeg 8: ${FFMPEG8_VERSION}"
ffmpeg8="${THIRDPARTY_ROOT}/FFmpeg8/ffmpeg8.elf"
ffprobe8="${THIRDPARTY_ROOT}/FFmpeg8/ffprobe8.elf"
ffmpeg8_amd="${THIRDPARTY_ROOT}/FFmpeg8/ffmpeg8-amd.sh"
ffmpeg8_version="$(${ffmpeg8} -version 2>&1)" || { echo "${ffmpeg8_version}" >&2; exit 1; }
echo "${ffmpeg8_version}"
echo "${ffmpeg8_version}" | grep -F "ffmpeg version n${FFMPEG8_VERSION}"
"${ffprobe8}" -version 2>&1 | grep -F "ffprobe version n${FFMPEG8_VERSION}"

ffmpeg8_buildconf="$(${ffmpeg8} -buildconf 2>&1)"
for option in \
    --enable-amf --enable-cuvid --enable-ffnvcodec --enable-libvpl \
    --enable-nvdec --enable-nvenc --enable-vaapi --enable-filter=deinterlace_vaapi \
    --enable-opencl --enable-cuda-llvm; do
    echo "${ffmpeg8_buildconf}" | grep -F -- "${option}"
done
for option in --enable-nonfree --enable-cuda-nvcc; do
    if echo "${ffmpeg8_buildconf}" | grep -F -- "${option}"; then
        echo "Unexpected configure option: ${option}" >&2
        exit 1
    fi
done
if echo "${ffmpeg8_version}" | grep -F 'nonfree and unredistributable'; then
    echo 'FFmpeg 8 must remain redistributable.' >&2
    exit 1
fi

ffmpeg8_hwaccels="$(${ffmpeg8} -hide_banner -hwaccels 2>&1)"
for hwaccel in amf cuda qsv vaapi vdpau; do
    echo "${ffmpeg8_hwaccels}" | grep -Fx "${hwaccel}"
done

ffmpeg8_decoders="$(${ffmpeg8} -hide_banner -decoders 2>&1)"
for decoder in av1_amf h264_amf hevc_amf av1_cuvid h264_cuvid hevc_cuvid av1_qsv h264_qsv hevc_qsv; do
    echo "${ffmpeg8_decoders}" | grep -Eq "[[:space:]]${decoder}[[:space:]]" || { echo "Missing decoder: ${decoder}" >&2; exit 1; }
done

ffmpeg8_encoders="$(${ffmpeg8} -hide_banner -encoders 2>&1)"
for encoder in av1_amf h264_amf hevc_amf av1_nvenc h264_nvenc hevc_nvenc av1_qsv h264_qsv hevc_qsv; do
    echo "${ffmpeg8_encoders}" | grep -Eq "[[:space:]]${encoder}[[:space:]]" || { echo "Missing encoder: ${encoder}" >&2; exit 1; }
done

ffmpeg8_filters="$(${ffmpeg8} -hide_banner -filters 2>&1)"
for filter in deinterlace_vaapi format hwdownload hwmap hwupload hwupload_cuda scale_cuda yadif_cuda bwdif_cuda scale_qsv scale_vaapi vpp_amf; do
    echo "${ffmpeg8_filters}" | grep -Eq "[[:space:]]${filter}[[:space:]]" || { echo "Missing filter: ${filter}" >&2; exit 1; }
done

ffmpeg8_deinterlace_vaapi_help="$(${ffmpeg8} -hide_banner -h filter=deinterlace_vaapi 2>&1)"
for option in motion_compensated motion_adaptive bob rate frame field auto; do
    echo "${ffmpeg8_deinterlace_vaapi_help}" | grep -Eq "[[:space:]]${option}([[:space:]]|$)" || {
        echo "Missing deinterlace_vaapi option: ${option}" >&2
        exit 1
    }
done

bash -n "${ffmpeg8_amd}"
ffmpeg8_amd_version="$(${ffmpeg8_amd} -version 2>&1)" || { echo "${ffmpeg8_amd_version}" >&2; exit 1; }
echo "${ffmpeg8_amd_version}" | grep -F "ffmpeg version n${FFMPEG8_VERSION}"
grep -Fx 'export LIBVA_DRIVER_NAME=radeonsi' "${ffmpeg8_amd}"
grep -Fx 'export LIBVA_DRIVERS_PATH=/opt/amdgpu/lib/x86_64-linux-gnu/dri' "${ffmpeg8_amd}"
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
    FFmpeg8/ffmpeg8.elf \
    FFmpeg8/ffprobe8.elf \
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

test "$(patchelf --print-rpath "${THIRDPARTY_ROOT}/FFmpeg8/ffmpeg8.elf")" = '$ORIGIN'
test "$(patchelf --print-rpath "${THIRDPARTY_ROOT}/FFmpeg8/ffprobe8.elf")" = '$ORIGIN'
test "$(patchelf --print-rpath "${THIRDPARTY_ROOT}/QSVEncC/QSVEncC.elf")" = '$ORIGIN:$ORIGIN/../Library'
test ! -d "${THIRDPARTY_ROOT}/Library/lib"
test ! -d "${THIRDPARTY_ROOT}/Library/usr"
