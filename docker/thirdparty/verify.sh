#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
THIRDPARTY_ROOT="${1:-/opt/thirdparty}"

set -a
source "${SCRIPT_DIR}/manifest.env"
set +a

case "${NONFREE:-}" in
    true)
        expected_media_driver_nonfree_kernels='ON'
        ;;
    false)
        expected_media_driver_nonfree_kernels='OFF'
        ;;
    *)
        echo "NONFREE must be exactly 'true' or 'false'. actual: ${NONFREE:-<unset>}" >&2
        exit 2
        ;;
esac

required_files=(
    Akebi/akebi-https-server.elf
    CMAnalysis/chapter_exe
    CMAnalysis/ffmsindex
    CMAnalysis/logoframe
    CMAnalysis/join_logo_scp
    CMAnalysis/libffms2.so
    CMAnalysis/libffms2.so.3
    CMAnalysis/libavisynth.so
    CMAnalysis/libavisynth.so.10
    CMAnalysis/libavcodec.so.62
    CMAnalysis/libavformat.so.62
    CMAnalysis/libavutil.so.60
    CMAnalysis/libswresample.so.6
    CMAnalysis/libswscale.so.9
    FFmpeg8/ffmpeg8-amd.sh
    FFmpeg8/ffmpeg8.elf
    FFmpeg8/ffprobe8.elf
    KonomiTVBS4KTLVMetadata/KonomiTVBS4KTLVMetadata.elf
    Python/bin/python
    psisiarc/psisiarc.elf
    tsreadex/tsreadex.elf
    Library/dri/iHD_drv_video.so
    Library/libmfx-gen.so.1.2
    Library/libmfxhw64.so.1
    Library/libva.so.2
    Library/libva-drm.so.2
    Library/libva-x11.so.2
)

for relative_path in "${required_files[@]}"; do
    echo "Verifying file: ${relative_path}"
    test -s "${THIRDPARTY_ROOT}/${relative_path}" || { echo "Missing file: ${relative_path}" >&2; exit 1; }
    file "${THIRDPARTY_ROOT}/${relative_path}"
    file "${THIRDPARTY_ROOT}/${relative_path}" | grep -Eq 'ELF 64-bit|Python script|shell script|symbolic link'
done

required_cm_analysis_documents=(
    CMAnalysis/JL/JL_標準.txt
    CMAnalysis/FFmpeg-Build-Configuration.txt
    CMAnalysis/Runtime-Manifest.json
    CMAnalysis/License-FFmpeg-LGPLv2.1.txt
    CMAnalysis/License-AviSynthPlus-GPLv2.txt
    CMAnalysis/License-FFMS2-MIT.txt
    CMAnalysis/License-chapter_exe-GPLv2.txt
    CMAnalysis/License-logoframe-GPLv2.txt
    CMAnalysis/License-join_logo_scp-GPLv2.txt
    Library/Intel-Media-Driver-Build-Configuration.txt
)
for relative_path in "${required_cm_analysis_documents[@]}"; do
    test -s "${THIRDPARTY_ROOT}/${relative_path}" || { echo "Missing license/runtime document: ${relative_path}" >&2; exit 1; }
done

required_mmt_tlv_documents=(
    FFmpeg8/License-libaribtlv-MIT.txt
    FFmpeg8/License-ffmpeg-libaribtlv-MIT.txt
    KonomiTVBS4KTLVMetadata/License-libaribtlv-MIT.txt
)
for relative_path in "${required_mmt_tlv_documents[@]}"; do
    test -s "${THIRDPARTY_ROOT}/${relative_path}" || {
        echo "Missing MMT/TLV license document: ${relative_path}" >&2
        exit 1
    }
done

echo 'Verifying the Amatsukaze-free CM analysis runtime layout.'
test ! -e "${THIRDPARTY_ROOT}/Amatsukaze"
test ! -e "${THIRDPARTY_ROOT}/CMAnalysis/logoframe.ini"
if find "${THIRDPARTY_ROOT}/CMAnalysis" -maxdepth 1 -iname '*amatsukaze*' -print -quit | grep -q .; then
    echo 'Amatsukaze artifact found in CMAnalysis.' >&2
    exit 1
fi
cm_ffmpeg_configuration="${THIRDPARTY_ROOT}/CMAnalysis/FFmpeg-Build-Configuration.txt"
grep -Fx "source_commit=${FFMPEG8_COMMIT}" "${cm_ffmpeg_configuration}"
grep -Fx 'license=LGPL-2.1-or-later' "${cm_ffmpeg_configuration}"
grep -Fx 'CONFIG_GPL=0' "${cm_ffmpeg_configuration}"
grep -Fx 'CONFIG_VERSION3=0' "${cm_ffmpeg_configuration}"
grep -Fx 'CONFIG_NONFREE=0' "${cm_ffmpeg_configuration}"
cm_ffmpeg_configure_sha256="$(sed -n 's/^configure_sha256=//p' "${cm_ffmpeg_configuration}")"
test "${#cm_ffmpeg_configure_sha256}" -eq 64
test "$(sed -n 's/^configure_option=//p' "${cm_ffmpeg_configuration}" | sha256sum | cut -d' ' -f1)" = \
    "${cm_ffmpeg_configure_sha256}"
for forbidden_option in --enable-gpl --enable-version3 --enable-nonfree --enable-libx264 --enable-libx265; do
    if grep -Fx "configure_option=${forbidden_option}" "${cm_ffmpeg_configuration}"; then
        echo "Forbidden CM FFmpeg configure option: ${forbidden_option}" >&2
        exit 1
    fi
done
for required_option in \
    --enable-decoders --enable-demuxer=wav --enable-decoder=pcm_s16le --enable-libaom; do
    grep -Fx "configure_option=${required_option}" "${cm_ffmpeg_configuration}"
done
LD_LIBRARY_PATH="${THIRDPARTY_ROOT}/CMAnalysis:${THIRDPARTY_ROOT}/Library" \
    ldd "${THIRDPARTY_ROOT}/CMAnalysis/libavcodec.so.62" | \
    grep -E 'libaom\.so\.3 => /'

cm_runtime_manifest="${THIRDPARTY_ROOT}/CMAnalysis/Runtime-Manifest.json"
python3 -m json.tool "${cm_runtime_manifest}" > /dev/null
grep -Fq "\"ffmpeg\": {\"version\": \"${FFMPEG8_VERSION}\", \"commit\": \"${FFMPEG8_COMMIT}\"" \
    "${cm_runtime_manifest}"
grep -Fq "\"avisynthplus\": {\"version\": \"${AVISYNTHPLUS_TAG#v}\", \"commit\": \"${AVISYNTHPLUS_COMMIT}\", \"profile\": \"shared-core-only\"}" \
    "${cm_runtime_manifest}"
grep -Fq "\"ffms2\": {\"version\": \"${FFMS2_VERSION}\", \"commit\": \"${FFMS2_COMMIT}\", \"profile\": \"avisynth-only-hardware\"}" \
    "${cm_runtime_manifest}"
for expected_value in \
    "${CHAPTER_EXE_COMMIT}" "${LOGOFRAME_COMMIT}" "${JOIN_LOGO_SCP_COMMIT}" \
    "${JOIN_LOGO_SCP_COMMAND_SHA256}" \
    "${FFMS2_HARDWARE_DECODING_PATCH_SHA256}" \
    "${CHAPTER_EXE_AVISYNTH_INIT_PATCH_SHA256}" "${LOGOFRAME_ERROR_LIFETIME_PATCH_SHA256}" \
    "${LOGOFRAME_PARALLEL_SCAN_PATCH_SHA256}" "${LOGOFRAME_NATIVE_LUMA_PATCH_SHA256}" \
    "${LOGOFRAME_HIGH_BIT_RGB_FALLBACK_PATCH_SHA256}"; do
    grep -Fq "${expected_value}" "${cm_runtime_manifest}"
done
grep -Fq "\"configure_sha256\": \"${cm_ffmpeg_configure_sha256}\"" "${cm_runtime_manifest}"
if grep -Eq '(/build/|/opt/|/home/)' "${cm_runtime_manifest}"; then
    echo 'CM runtime manifest contains a build-host path.' >&2
    exit 1
fi

echo "Verifying the Intel Media Driver build configuration for NONFREE=${NONFREE}."
intel_media_driver_configuration="${THIRDPARTY_ROOT}/Library/Intel-Media-Driver-Build-Configuration.txt"
grep -Fx "source_commit=${INTEL_MEDIA_DRIVER_COMMIT}" "${intel_media_driver_configuration}"
grep -Fx "nonfree=${NONFREE}" "${intel_media_driver_configuration}"
grep -Fx 'ENABLE_KERNELS:BOOL=ON' "${intel_media_driver_configuration}"
grep -Fx "ENABLE_NONFREE_KERNELS:BOOL=${expected_media_driver_nonfree_kernels}" "${intel_media_driver_configuration}"
grep -Fx 'BUILD_CMRTLIB:BOOL=OFF' "${intel_media_driver_configuration}"
grep -Fx 'cmrt_ENABLE_KERNELS:BOOL=ON' "${intel_media_driver_configuration}"
grep -Fx 'cmrt_ENABLE_NONFREE_KERNELS:BOOL=OFF' "${intel_media_driver_configuration}"
grep -Fx 'cmrt_BUILD_CMRTLIB:BOOL=ON' "${intel_media_driver_configuration}"
grep -Fx "patch_intel_libva_standalone_sha256=${INTEL_LIBVA_STANDALONE_PATCH_SHA256}" \
    "${intel_media_driver_configuration}"
grep -Fx "patch_intel_media_driver_vpp_deinterlace_crash_fix_sha256=${INTEL_MEDIA_DRIVER_VPP_DEINTERLACE_CRASH_FIX_PATCH_SHA256}" \
    "${intel_media_driver_configuration}"
grep -Fx "patch_intel_onevpl_gpu_rt_vpp_deinterlace_hang_fix_sha256=${INTEL_ONEVPL_GPU_RT_VPP_DEINTERLACE_HANG_FIX_PATCH_SHA256}" \
    "${intel_media_driver_configuration}"
if [ "${NONFREE}" = 'false' ]; then
    grep -Fx 'compile_definition=_FULL_OPEN_SOURCE' "${intel_media_driver_configuration}"
elif grep -Fx 'compile_definition=_FULL_OPEN_SOURCE' "${intel_media_driver_configuration}"; then
    echo 'Full Feature media-driver configuration unexpectedly records _FULL_OPEN_SOURCE.' >&2
    exit 1
fi
grep -Fx 'compile_definition=_MPEG2_DECODE_SUPPORTED' "${intel_media_driver_configuration}"
grep -Fx 'compile_definition=_AVC_DECODE_SUPPORTED' "${intel_media_driver_configuration}"
grep -Fx 'compile_definition=_HEVC_DECODE_SUPPORTED' "${intel_media_driver_configuration}"

echo 'Verifying the bundled Intel runtime dynamic dependency closure.'
intel_library_root="${THIRDPARTY_ROOT}/Library"
intel_runtime_candidates=(
    "${intel_library_root}/dri/iHD_drv_video.so"
    "${intel_library_root}/libmfxhw64.so.1"
)
while IFS= read -r runtime_file; do
    intel_runtime_candidates+=("${runtime_file}")
done < <(find "${intel_library_root}" -maxdepth 1 \( -type f -o -type l \) \
    \( -name 'libmfx-gen.so*' -o -name 'libva*.so*' \) -print | sort)

declare -A verified_intel_runtime_files=()
for runtime_file in "${intel_runtime_candidates[@]}"; do
    resolved_runtime_file="$(readlink -f "${runtime_file}")"
    test -n "${resolved_runtime_file}"
    if [ -n "${verified_intel_runtime_files[${resolved_runtime_file}]:-}" ]; then
        continue
    fi
    verified_intel_runtime_files["${resolved_runtime_file}"]=1

    echo "Verifying Intel runtime dependency closure: ${runtime_file}"
    runtime_dependencies="$(LD_LIBRARY_PATH="${intel_library_root}" ldd "${resolved_runtime_file}")"
    echo "${runtime_dependencies}"
    if grep -F 'not found' <<< "${runtime_dependencies}"; then
        echo "Intel runtime has unresolved dynamic dependencies: ${runtime_file}" >&2
        exit 1
    fi
    while read -r dependency separator resolved_dependency _; do
        case "${dependency}" in
            libigdgmm.so*|libigfxcmrt.so*|libmfx*.so*|libva*.so*)
                if [ "${separator}" != '=>' ] || [[ "${resolved_dependency}" != "${intel_library_root}/"* ]]; then
                    echo "Intel dependency did not resolve from bundled Library: ${dependency} => ${resolved_dependency}" >&2
                    exit 1
                fi
                ;;
        esac
    done <<< "${runtime_dependencies}"
done

echo 'Verifying component versions.'
cm_root="${THIRDPARTY_ROOT}/CMAnalysis"
logoframe_status=0
logoframe_help="$("${cm_root}/logoframe" -h 2>&1)" || logoframe_status=$?
test "${logoframe_status}" -eq 2
grep -F "logoframe ${LOGOFRAME_VERSION}" <<< "${logoframe_help}"
test "$(readlink "${cm_root}/libavisynth.so")" = 'libavisynth.so.10'
for cm_binary in \
    chapter_exe ffmsindex logoframe join_logo_scp libffms2.so.3 libavisynth.so.10 \
    libavcodec.so.62 libavformat.so.62 libavutil.so.60 libswresample.so.6 libswscale.so.9; do
    test "$(patchelf --print-rpath "${cm_root}/${cm_binary}")" = '$ORIGIN'
done

ffms2_dependencies="$(ldd "${cm_root}/libffms2.so.3")"
echo "${ffms2_dependencies}"
for library in libavcodec libavformat libavutil libswresample libswscale; do
    resolved_library="$(awk -v library="${library}" '$1 ~ ("^" library "\\.so") { print $3; exit }' <<< "${ffms2_dependencies}")"
    if [[ "${resolved_library}" != "${cm_root}/"* ]]; then
        echo "FFMS2 did not resolve ${library} from CMAnalysis: ${resolved_library}" >&2
        exit 1
    fi
done
nm -D "${cm_root}/libffms2.so.3" | grep -F ' FFMS_CreateVideoSource2'
strings "${cm_root}/logoframe" | grep -F 'native luma:'
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
    --enable-gpl --enable-version3 \
    --enable-libaribtlv \
    --enable-muxer=wav --enable-encoder=pcm_s16le \
    --enable-filter=aresample --enable-filter=asetpts \
    --enable-amf --enable-cuvid --enable-ffnvcodec --enable-libvpl \
    --enable-nvdec --enable-nvenc --enable-vaapi --enable-filter=deinterlace_vaapi \
    --enable-opencl --enable-cuda-llvm; do
    echo "${ffmpeg8_buildconf}" | grep -F -- "${option}"
done

ffmpeg8_demuxers="$(${ffprobe8} -hide_banner -demuxers 2>&1)"
grep -Eq '[[:space:]]libaribtlv[[:space:]]' <<< "${ffmpeg8_demuxers}" || {
    echo 'Missing demuxer: libaribtlv' >&2
    exit 1
}
if ldd "${ffmpeg8}" | grep -F 'libaribtlv'; then
    echo 'libaribtlv must be statically linked into FFmpeg 8.' >&2
    exit 1
fi
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
    grep -Eq "[[:space:]]${decoder}[[:space:]]" <<< "${ffmpeg8_decoders}" || { echo "Missing decoder: ${decoder}" >&2; exit 1; }
done

ffmpeg8_encoders="$(${ffmpeg8} -hide_banner -encoders 2>&1)"
for encoder in pcm_s16le av1_amf h264_amf hevc_amf av1_nvenc h264_nvenc hevc_nvenc av1_qsv h264_qsv hevc_qsv; do
    grep -Eq "[[:space:]]${encoder}[[:space:]]" <<< "${ffmpeg8_encoders}" || { echo "Missing encoder: ${encoder}" >&2; exit 1; }
done
grep -Eq '[[:space:]]libwebp[[:space:]]' <<< "${ffmpeg8_encoders}" || {
    echo 'Missing FFmpeg 8 WebP encoder.' >&2
    exit 1
}

ffmpeg8_muxers="$(${ffmpeg8} -hide_banner -muxers 2>&1)"
grep -Eq '[[:space:]]wav[[:space:]]' <<< "${ffmpeg8_muxers}" || {
    echo 'Missing muxer: wav' >&2
    exit 1
}

ffmpeg8_filters="$(${ffmpeg8} -hide_banner -filters 2>&1)"
for filter in aresample asetpts deinterlace_vaapi format hwdownload hwmap hwupload hwupload_cuda scale_cuda yadif_cuda bwdif_cuda scale_qsv scale_vaapi vpp_amf; do
    grep -Eq "[[:space:]]${filter}[[:space:]]" <<< "${ffmpeg8_filters}" || { echo "Missing filter: ${filter}" >&2; exit 1; }
done

ffmpeg8_deinterlace_vaapi_help="$(${ffmpeg8} -hide_banner -h filter=deinterlace_vaapi 2>&1)"
for option in motion_compensated motion_adaptive bob rate frame field auto; do
    grep -Eq "[[:space:]]${option}([[:space:]]|$)" <<< "${ffmpeg8_deinterlace_vaapi_help}" || {
        echo "Missing deinterlace_vaapi option: ${option}" >&2
        exit 1
    }
done

bash -n "${ffmpeg8_amd}"
ffmpeg8_amd_version="$(${ffmpeg8_amd} -version 2>&1)" || { echo "${ffmpeg8_amd_version}" >&2; exit 1; }
echo "${ffmpeg8_amd_version}" | grep -F "ffmpeg version n${FFMPEG8_VERSION}"
grep -Fx 'export LIBVA_DRIVER_NAME=radeonsi' "${ffmpeg8_amd}"
grep -Fx 'if [ -e /opt/amdgpu/lib/x86_64-linux-gnu/dri/radeonsi_drv_video.so ]; then' "${ffmpeg8_amd}"
grep -Fx '    export LIBVA_DRIVERS_PATH=/opt/amdgpu/lib/x86_64-linux-gnu/dri' "${ffmpeg8_amd}"
grep -Fx '    export LIBVA_DRIVERS_PATH=/usr/lib/x86_64-linux-gnu/dri' "${ffmpeg8_amd}"
echo "Expected Python: ${PYTHON_VERSION}"
"${THIRDPARTY_ROOT}/Python/bin/python" --version | grep -F "Python ${PYTHON_VERSION}"
echo "Expected Poetry: ${POETRY_VERSION}"
"${THIRDPARTY_ROOT}/Python/bin/python" -m poetry --version | grep -F "Poetry (version ${POETRY_VERSION})"

for executable in \
    CMAnalysis/chapter_exe \
    CMAnalysis/ffmsindex \
    CMAnalysis/logoframe \
    CMAnalysis/join_logo_scp \
    CMAnalysis/libffms2.so.3 \
    CMAnalysis/libavisynth.so.10 \
    CMAnalysis/libavcodec.so.62 \
    CMAnalysis/libavformat.so.62 \
    CMAnalysis/libavutil.so.60 \
    CMAnalysis/libswresample.so.6 \
    CMAnalysis/libswscale.so.9 \
    FFmpeg8/ffmpeg8.elf \
    FFmpeg8/ffprobe8.elf \
    KonomiTVBS4KTLVMetadata/KonomiTVBS4KTLVMetadata.elf; do
    echo "Verifying dynamic dependencies: ${executable}"
    missing_dependencies="$(ldd "${THIRDPARTY_ROOT}/${executable}" | awk '/not found/ { print $1 }')"
    if [ -n "${missing_dependencies}" ]; then
        ldd "${THIRDPARTY_ROOT}/${executable}" >&2
        exit 1
    fi
done

test "$(patchelf --print-rpath "${THIRDPARTY_ROOT}/FFmpeg8/ffmpeg8.elf")" = '$ORIGIN'
test "$(patchelf --print-rpath "${THIRDPARTY_ROOT}/FFmpeg8/ffprobe8.elf")" = '$ORIGIN'
# 完成 artifact に旧 FFmpeg 7 と独立ハードウェアエンコーダーのディレクトリが再混入していないことを保証する
for removed_directory in FFmpeg QSVEncC NVEncC VCEEncC; do
    echo "Verifying removed artifact directory: ${removed_directory}"
    if [ -e "${THIRDPARTY_ROOT}/${removed_directory}" ]; then
        echo "Removed artifact directory still exists: ${removed_directory}" >&2
        exit 1
    fi
done
test ! -d "${THIRDPARTY_ROOT}/Library/lib"
test ! -d "${THIRDPARTY_ROOT}/Library/usr"

echo 'Verifying the isolated playback FFmpeg media environment.'
for playback_executable in "${ffmpeg8}" "${ffprobe8}"; do
    playback_dependencies="$(ldd "${playback_executable}")"
    while read -r dependency separator resolved_dependency _; do
        case "${dependency}" in
            libav*.so*|libsw*.so*)
                if [ "${separator}" != '=>' ] || [[ "${resolved_dependency}" != "${THIRDPARTY_ROOT}/FFmpeg8/"* ]]; then
                    echo "Playback FFmpeg dependency escaped FFmpeg8: ${dependency} => ${resolved_dependency}" >&2
                    exit 1
                fi
                ;;
        esac
    done <<< "${playback_dependencies}"
done

echo 'Verifying variable-channel playback audio normalization and the isolated FFMS2/chapter_exe runtime.'
runtime_smoke_root="$(mktemp -d)"
trap 'rm -rf "${runtime_smoke_root}"' EXIT

# 2ch and 5.1ch AAC segments use the same MPEG-TS PID.  Concatenating the raw TS
# packets makes FFmpeg reinitialize one decoded audio stream at the format boundary,
# reproducing the path where an unnormalized source can change audio layout midstream.
"${ffmpeg8}" -hide_banner -loglevel error \
    -f lavfi -i testsrc2=size=640x360:rate=30000/1001 \
    -f lavfi -i sine=frequency=440:sample_rate=48000 \
    -map 0:v:0 -map 1:a:0 \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p \
    -af 'pan=stereo|c0=c0|c1=c0' -frames:a 94 -c:a aac -b:a 192k \
    -streamid 0:256 -streamid 1:257 -f mpegts \
    "${runtime_smoke_root}/stereo.ts"
"${ffmpeg8}" -hide_banner -loglevel error \
    -f lavfi -i 'testsrc2=size=640x360:rate=30000/1001,drawbox=x=16:y=16:w=32:h=16:color=white@0.75:t=4,drawbox=x=20:y=20:w=24:h=8:color=black@0.60:t=fill' \
    -f lavfi -i anullsrc=channel_layout=5.1:sample_rate=48000 \
    -map 0:v:0 -map 1:a:0 \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p \
    -frames:a 94 -c:a aac -b:a 384k \
    -streamid 0:256 -streamid 1:257 -f mpegts \
    "${runtime_smoke_root}/surround.ts"
for audio_fixture in stereo surround; do
    fixture_pid="$("${ffprobe8}" -v error -select_streams a:0 \
        -show_entries stream=id -of csv=p=0 "${runtime_smoke_root}/${audio_fixture}.ts" | \
        sed -n '/./p' | sort -u)"
    test "${fixture_pid}" = '0x101'
done
cp "${runtime_smoke_root}/stereo.ts" "${runtime_smoke_root}/variable-channel.ts"
dd if="${runtime_smoke_root}/surround.ts" \
    of="${runtime_smoke_root}/variable-channel.ts" oflag=append conv=notrunc status=none

variable_channel_frames="$("${ffprobe8}" -v error -select_streams a:0 -show_frames \
    -show_entries frame=nb_samples,channels,channel_layout -of csv=p=0 \
    "${runtime_smoke_root}/variable-channel.ts")"
test "$(grep -Fxc '1024,2,stereo' <<< "${variable_channel_frames}")" -eq 95
test "$(grep -Fxc '1024,6,5.1' <<< "${variable_channel_frames}")" -eq 95
expected_normalized_audio_samples=$((190 * 1024))

# Keep this one-input/two-output command aligned with GenericCMAnalyzer._prepareMedia().
# A standalone WAV muxer appends samples sequentially across the decoder's channel-layout
# reinitialization; placing the same PCM track in Matroska would collapse the second segment.
"${ffmpeg8}" -hide_banner -loglevel error -nostdin -y \
    -fflags '+genpts+discardcorrupt' \
    -i "${runtime_smoke_root}/variable-channel.ts" \
    -map 0:0 -an -sn -dn \
    -c:v copy \
    -map_metadata -1 -map_chapters -1 \
    -f matroska \
    "${runtime_smoke_root}/prepared.cmwork" \
    -map 0:1 -vn -sn -dn \
    -filter:a 'asetpts=PTS-STARTPTS,aresample=48000:async=1000:first_pts=0' \
    -ac 1 -ar 48000 -sample_fmt s16 -c:a pcm_s16le \
    -map_metadata -1 -map_chapters -1 \
    -rf64 auto \
    -f wav \
    "${runtime_smoke_root}/normalized-audio.wav"
normalized_audio_probe="$("${ffprobe8}" -v error -select_streams a:0 \
    -show_entries stream=codec_name,sample_fmt,sample_rate,channels,duration_ts \
    -of default=noprint_wrappers=1 "${runtime_smoke_root}/normalized-audio.wav")"
for expected_audio_property in \
    codec_name=pcm_s16le sample_fmt=s16 sample_rate=48000 channels=1 \
    "duration_ts=${expected_normalized_audio_samples}"; do
    grep -Fx "${expected_audio_property}" <<< "${normalized_audio_probe}"
done
prepared_stream_types="$("${ffprobe8}" -v error -show_entries stream=codec_type \
    -of csv=p=0 "${runtime_smoke_root}/prepared.cmwork")"
test "$(grep -Fxc video <<< "${prepared_stream_types}")" -eq 1
test "$(grep -Fxc audio <<< "${prepared_stream_types}")" -eq 0
LD_LIBRARY_PATH="${cm_root}:${THIRDPARTY_ROOT}/Library" \
    "${cm_root}/ffmsindex" -f -t -1 \
        "${runtime_smoke_root}/prepared.cmwork" \
        "${runtime_smoke_root}/prepared.ffindex"
test -s "${runtime_smoke_root}/prepared.ffindex"
LD_LIBRARY_PATH="${cm_root}:${THIRDPARTY_ROOT}/Library" \
    "${cm_root}/ffmsindex" -f -t -1 \
        "${runtime_smoke_root}/normalized-audio.wav" \
        "${runtime_smoke_root}/normalized-audio.ffindex"
test -s "${runtime_smoke_root}/normalized-audio.ffindex"
prepared_video_frames="$("${ffprobe8}" -v error -count_frames -select_streams v:0 \
    -show_entries stream=nb_read_frames -of csv=p=0 \
    "${runtime_smoke_root}/prepared.cmwork" | sed -n '/^[0-9][0-9]*$/p')"
test -n "${prepared_video_frames}"
{
    echo 'ClearAutoloadDirs()'
    echo "LoadPlugin(\"${cm_root}/libffms2.so\")"
    echo "video = FFVideoSource(\"${runtime_smoke_root}/prepared.cmwork\", track=0, cache=true, cachefile=\"${runtime_smoke_root}/prepared.ffindex\", threads=2)"
    echo "audio = FFAudioSource(\"${runtime_smoke_root}/normalized-audio.wav\", track=0, cache=true, cachefile=\"${runtime_smoke_root}/normalized-audio.ffindex\", adjustdelay=-3, fill_gaps=1)"
    echo 'clip = AudioDubEx(video, audio)'
    echo 'clip = DelayAudio(clip, -0.001000000)'
    echo 'clip = ConvertBits(clip, 8)'
    echo 'clip = ConvertToYV12(clip)'
    echo 'clip = Prefetch(clip, 1)'
    echo 'return clip'
} > "${runtime_smoke_root}/chapter.avs"
LD_LIBRARY_PATH="${cm_root}:${THIRDPARTY_ROOT}/Library" \
    "${cm_root}/chapter_exe" \
        -v "${runtime_smoke_root}/chapter.avs" \
        -o "${runtime_smoke_root}/chapter-exe.txt" \
        -s 10 \
        > "${runtime_smoke_root}/chapter-exe.output" \
        2>&1
test -s "${runtime_smoke_root}/chapter-exe.txt"
grep -aF "Video Frames: ${prepared_video_frames}" \
    "${runtime_smoke_root}/chapter-exe.output"

echo 'Verifying that FFMS2 hardware failures are marked for CPU fallback.'
{
    echo 'ClearAutoloadDirs()'
    echo "LoadPlugin(\"${cm_root}/libffms2.so\")"
    echo "return FFVideoSource(\"${runtime_smoke_root}/prepared.cmwork\", track=0, cache=true, cachefile=\"${runtime_smoke_root}/prepared.ffindex\", hwdevice=\"invalid-device\")"
} > "${runtime_smoke_root}/invalid-hw.avs"
LD_LIBRARY_PATH="${cm_root}:${THIRDPARTY_ROOT}/Library" \
    "${cm_root}/chapter_exe" \
        -v "${runtime_smoke_root}/invalid-hw.avs" \
        -o "${runtime_smoke_root}/invalid-hw.txt" \
        -s 10 \
        > "${runtime_smoke_root}/invalid-hw.output" \
        2>&1 || true
# chapter_exe upstream may return zero after an AviSynth load failure. KonomiTV
# therefore detects the explicit FFMS2-HW marker in its combined process output.
grep -aF 'FFMS2-HW:' "${runtime_smoke_root}/invalid-hw.output"

echo 'Verifying MP4/MKV AVC, HEVC Main10, and AV1 Main10 through the production media path.'
python3 "${SCRIPT_DIR}/generate-cm-smoke-fixture.py" \
    "${runtime_smoke_root}/smoke-logo.lgd"
test -s "${runtime_smoke_root}/smoke-logo.lgd"
video_fixture_filter='testsrc2=size=640x360:rate=30000/1001,drawbox=x=16:y=16:w=32:h=16:color=white@0.75:t=4,drawbox=x=20:y=20:w=24:h=8:color=black@0.60:t=fill'
"${ffmpeg8}" -hide_banner -loglevel error \
    -f lavfi -i "${video_fixture_filter}" \
    -f lavfi -i sine=frequency=440:sample_rate=48000 \
    -map 0:v:0 -map 1:a:0 -frames:v 60 -shortest \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac -b:a 96k \
    "${runtime_smoke_root}/avc.mp4"
"${ffmpeg8}" -hide_banner -loglevel error \
    -f lavfi -i "${video_fixture_filter}" \
    -f lavfi -i sine=frequency=550:sample_rate=48000 \
    -map 0:v:0 -map 1:a:0 -frames:v 60 -shortest \
    -c:v libx265 -preset ultrafast -pix_fmt yuv420p10le \
    -x265-params 'pools=4:frame-threads=2:log-level=error' \
    -c:a aac -b:a 96k "${runtime_smoke_root}/hevc-main10.mkv"
"${ffmpeg8}" -hide_banner -loglevel error \
    -f lavfi -i "${video_fixture_filter}" \
    -f lavfi -i sine=frequency=660:sample_rate=48000 \
    -map 0:v:0 -map 1:a:0 -frames:v 60 -shortest \
    -c:v libaom-av1 -cpu-used 8 -row-mt 1 -threads 4 -lag-in-frames 0 -g 30 \
    -pix_fmt yuv420p10le -c:a aac -b:a 96k \
    "${runtime_smoke_root}/av1-main10.mkv"

verify_cm_media_fixture() {
    local fixture_name="$1"
    local source_path="$2"
    local expected_luma="$3"
    local prepared_path="${runtime_smoke_root}/${fixture_name}.prepared.cmwork"
    local audio_path="${runtime_smoke_root}/${fixture_name}.prepared.wav"
    local video_index="${runtime_smoke_root}/${fixture_name}.video.ffindex"
    local audio_index="${runtime_smoke_root}/${fixture_name}.audio.ffindex"
    local chapter_script="${runtime_smoke_root}/${fixture_name}.chapter.avs"
    local logo_script="${runtime_smoke_root}/${fixture_name}.logo.avs"

    # This remains intentionally identical to GenericCMAnalyzer._prepareMedia().
    "${ffmpeg8}" -hide_banner -loglevel error -nostdin -y \
        -fflags '+genpts+discardcorrupt' -i "${source_path}" \
        -map 0:0 -an -sn -dn -c:v copy \
        -map_metadata -1 -map_chapters -1 -f matroska "${prepared_path}" \
        -map 0:1 -vn -sn -dn \
        -filter:a 'asetpts=PTS-STARTPTS,aresample=48000:async=1000:first_pts=0' \
        -ac 1 -ar 48000 -sample_fmt s16 -c:a pcm_s16le \
        -map_metadata -1 -map_chapters -1 -rf64 auto -f wav "${audio_path}"
    LD_LIBRARY_PATH="${cm_root}:${THIRDPARTY_ROOT}/Library" \
        "${cm_root}/ffmsindex" -f -t -1 "${prepared_path}" "${video_index}"
    LD_LIBRARY_PATH="${cm_root}:${THIRDPARTY_ROOT}/Library" \
        "${cm_root}/ffmsindex" -f -t -1 "${audio_path}" "${audio_index}"
    local expected_frames
    expected_frames="$("${ffprobe8}" -v error -count_frames -select_streams v:0 \
        -show_entries stream=nb_read_frames -of csv=p=0 "${prepared_path}" | \
        sed -n '/^[0-9][0-9]*$/p')"
    test "${expected_frames}" = '60'
    {
        echo 'ClearAutoloadDirs()'
        echo "LoadPlugin(\"${cm_root}/libffms2.so\")"
        echo "video = FFVideoSource(\"${prepared_path}\", track=0, fpsnum=30000, fpsden=1001, cache=true, cachefile=\"${video_index}\", threads=2)"
        echo "audio = FFAudioSource(\"${audio_path}\", track=0, cache=true, cachefile=\"${audio_index}\", adjustdelay=-3, fill_gaps=1)"
        echo 'clip = AudioDubEx(video, audio)'
        echo 'clip = ConvertBits(clip, 8)'
        echo 'clip = ConvertToYV12(clip)'
        echo 'clip = Prefetch(clip, 1)'
        echo 'return clip'
    } > "${chapter_script}"
    LD_LIBRARY_PATH="${cm_root}:${THIRDPARTY_ROOT}/Library" \
        "${cm_root}/chapter_exe" -v "${chapter_script}" \
            -o "${runtime_smoke_root}/${fixture_name}.chapter.txt" -s 10 \
            > "${runtime_smoke_root}/${fixture_name}.chapter.output" 2>&1
    grep -aF "Video Frames: ${expected_frames}" \
        "${runtime_smoke_root}/${fixture_name}.chapter.output"

    {
        echo 'ClearAutoloadDirs()'
        echo "LoadPlugin(\"${cm_root}/libffms2.so\")"
        echo "clip = FFVideoSource(\"${prepared_path}\", track=0, fpsnum=30000, fpsden=1001, cache=true, cachefile=\"${video_index}\", threads=2)"
        echo 'clip = Prefetch(clip, 1)'
        echo 'return clip'
    } > "${logo_script}"
    LD_LIBRARY_PATH="${cm_root}:${THIRDPARTY_ROOT}/Library" \
        "${cm_root}/logoframe" "${logo_script}" \
            -oanum 1 -oasel 1 \
            -oa "${runtime_smoke_root}/${fixture_name}.logo.txt" \
            -parallel 2 -dispoff 1 -paramoff 1 \
            -mrgleft 0 -mrgright 0 -onwidth 5 -offwidth 5 -clrrate 0 \
            -logo1 "${runtime_smoke_root}/smoke-logo.lgd" \
            > "${runtime_smoke_root}/${fixture_name}.logo.stdout" \
            2> "${runtime_smoke_root}/${fixture_name}.logo.stderr"
    tr -d '\r' < "${runtime_smoke_root}/${fixture_name}.logo_list.ini" \
        > "${runtime_smoke_root}/${fixture_name}.logo-list.normalized.ini"
    grep -Fx "FrameTotal=${expected_frames}" \
        "${runtime_smoke_root}/${fixture_name}.logo-list.normalized.ini"
    grep -F "native luma: ${expected_luma}" \
        "${runtime_smoke_root}/${fixture_name}.logo.stderr"
}

verify_cm_media_fixture avc-mp4 "${runtime_smoke_root}/avc.mp4" '8-bit limited range'
verify_cm_media_fixture hevc-mkv "${runtime_smoke_root}/hevc-main10.mkv" '10-bit limited range'
verify_cm_media_fixture av1-mkv "${runtime_smoke_root}/av1-main10.mkv" '10-bit limited range'

echo 'Verifying the production-style FFMS2/native-luma parallel logo scan.'
{
    echo 'ClearAutoloadDirs()'
    echo "LoadPlugin(\"${cm_root}/libffms2.so\")"
    echo "video = FFVideoSource(\"${runtime_smoke_root}/prepared.cmwork\", track=0, fpsnum=30000, fpsden=1001, cache=true, cachefile=\"${runtime_smoke_root}/prepared.ffindex\", threads=2)"
    echo 'clip = video'
    echo 'clip = Prefetch(clip, 1)'
    echo 'return clip'
} > "${runtime_smoke_root}/logo.avs"
# Source/index/parallel options mirror production. The four-second fixture uses
# shorter edge-merge/detection windows so it can still emit a selected result.
logoframe_success_status=0
LD_LIBRARY_PATH="${cm_root}:${THIRDPARTY_ROOT}/Library" \
    "${cm_root}/logoframe" \
        "${runtime_smoke_root}/logo.avs" \
        -oanum 1 \
        -oasel 1 \
        -oa "${runtime_smoke_root}/logoframe-analysis.txt" \
        -parallel 2 \
        -dispoff 1 \
        -paramoff 1 \
        -mrgleft 0 \
        -mrgright 0 \
        -onwidth 5 \
        -offwidth 5 \
        -clrrate 0 \
        -logo1 "${runtime_smoke_root}/smoke-logo.lgd" \
        > "${runtime_smoke_root}/logoframe-success.stdout" \
        2> "${runtime_smoke_root}/logoframe-success.stderr" || logoframe_success_status=$?
if [ "${logoframe_success_status}" -ne 0 ]; then
    cat "${runtime_smoke_root}/logoframe-success.stdout" >&2
    cat "${runtime_smoke_root}/logoframe-success.stderr" >&2
    exit "${logoframe_success_status}"
fi
test -s "${runtime_smoke_root}/logoframe-analysis.txt" || {
    echo 'logoframe did not write its selected analysis output.' >&2
    cat "${runtime_smoke_root}/logoframe-success.stderr" >&2
    if [ -e "${runtime_smoke_root}/logoframe-analysis_list.ini" ]; then
        cat "${runtime_smoke_root}/logoframe-analysis_list.ini" >&2
    fi
    exit 1
}
test -s "${runtime_smoke_root}/logoframe-analysis_list.ini" || {
    echo 'logoframe did not write its analysis list.' >&2
    exit 1
}
tr -d '\r' < "${runtime_smoke_root}/logoframe-analysis_list.ini" \
    > "${runtime_smoke_root}/logoframe-analysis-list.normalized.ini"
grep -Fx "FrameTotal=${prepared_video_frames}" \
    "${runtime_smoke_root}/logoframe-analysis-list.normalized.ini"
grep -F 'native luma: 8-bit limited range' \
    "${runtime_smoke_root}/logoframe-success.stderr"

echo 'Verifying that logoframe fails cleanly on an invalid AviSynth script.'
printf '%s\n' 'ThisFunctionMustNotExist()' > "${runtime_smoke_root}/invalid.avs"
logoframe_failure_status=0
LD_LIBRARY_PATH="${cm_root}:${THIRDPARTY_ROOT}/Library" \
    "${cm_root}/logoframe" \
        "${runtime_smoke_root}/invalid.avs" \
        -logo "${runtime_smoke_root}/missing.lgd" \
        -oa "${runtime_smoke_root}/must-not-exist.txt" \
        > "${runtime_smoke_root}/logoframe.stdout" \
        2> "${runtime_smoke_root}/logoframe.stderr" || logoframe_failure_status=$?
test "${logoframe_failure_status}" -eq 1
grep -F 'Avisynth error:' "${runtime_smoke_root}/logoframe.stderr"
test ! -e "${runtime_smoke_root}/must-not-exist.txt"
rm -rf "${runtime_smoke_root}"
trap - EXIT
