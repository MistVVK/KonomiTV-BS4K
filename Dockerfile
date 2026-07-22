# syntax=docker/dockerfile:1.7

# --------------------------------------------------------------------------------------------------------------
# サードパーティー実行環境を固定構築するステージ
# --------------------------------------------------------------------------------------------------------------

FROM ubuntu:22.04@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982 AS thirdparty-builder

ARG CUDA_VERSION=12.4
ARG NONFREE=true
ENV DEBIAN_FRONTEND=noninteractive

RUN case "${CUDA_VERSION}" in \
        '12.4') CUDA_PACKAGE_SUFFIX='12-4' ;; \
        '12.8') CUDA_PACKAGE_SUFFIX='12-8' ;; \
        *) echo 'CUDA_VERSION must be either 12.4 or 12.8.' >&2; exit 1 ;; \
    esac && \
    case "${NONFREE}" in \
        'true'|'false') ;; \
        *) echo 'NONFREE must be either true or false.' >&2; exit 1 ;; \
    esac && \
    apt-get update && apt-get install -y --no-install-recommends ca-certificates nala && \
    printf '%s\n' \
        'deb https://ftp.udx.icscoe.jp/Linux/ubuntu/ jammy main restricted universe multiverse' \
        'deb https://ftp.udx.icscoe.jp/Linux/ubuntu/ jammy-updates main restricted universe multiverse' \
        'deb https://ftp.udx.icscoe.jp/Linux/ubuntu/ jammy-backports main restricted universe multiverse' \
        'deb https://ftp.udx.icscoe.jp/Linux/ubuntu/ jammy-security main restricted universe multiverse' \
        'deb https://ftp.riken.jp/Linux/ubuntu/ jammy main restricted universe multiverse' \
        'deb https://ftp.riken.jp/Linux/ubuntu/ jammy-updates main restricted universe multiverse' \
        'deb https://ftp.riken.jp/Linux/ubuntu/ jammy-backports main restricted universe multiverse' \
        'deb https://ftp.riken.jp/Linux/ubuntu/ jammy-security main restricted universe multiverse' \
        'deb https://ftp.tsukuba.wide.ad.jp/Linux/ubuntu/ jammy main restricted universe multiverse' \
        'deb https://ftp.tsukuba.wide.ad.jp/Linux/ubuntu/ jammy-updates main restricted universe multiverse' \
        'deb https://ftp.tsukuba.wide.ad.jp/Linux/ubuntu/ jammy-backports main restricted universe multiverse' \
        'deb https://ftp.tsukuba.wide.ad.jp/Linux/ubuntu/ jammy-security main restricted universe multiverse' \
        > /etc/apt/sources.list && \
    nala update && nala install -y --no-install-recommends curl gpg && \
    curl -fsSL https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb \
        --output /tmp/cuda-keyring.deb && \
    echo 'd93190d50b98ad4699ff40f4f7af50f16a76dac3bb8da1eaaf366d47898ff8df  /tmp/cuda-keyring.deb' | sha256sum --check - && \
    dpkg --install /tmp/cuda-keyring.deb && \
    rm /tmp/cuda-keyring.deb && \
    curl -fsSL https://repositories.intel.com/gpu/intel-graphics.key | gpg --yes --dearmor --output /usr/share/keyrings/intel-graphics-keyring.gpg && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics-keyring.gpg] https://repositories.intel.com/gpu/ubuntu jammy/lts/2523 unified' > /etc/apt/sources.list.d/intel-gpu-jammy.list && \
    nala update && nala upgrade -y && nala install -y --no-install-recommends \
        autoconf automake build-essential ca-certificates ccache clang cmake cuda-cudart-dev-${CUDA_PACKAGE_SUFFIX} cuda-nvvm-${CUDA_PACKAGE_SUFFIX} curl file git libdrm-dev libffi-dev libglib2.0-dev \
        libaom-dev libass-dev libbluray-dev libbz2-dev libfontconfig1-dev libfreetype6-dev libfribidi-dev \
        libgnutls28-dev libgsm1-dev liblzma-dev libmp3lame-dev libmysofa-dev \
        libnuma-dev libopenjp2-7-dev libopenmpt-dev libopus-dev libpciaccess-dev librabbitmq-dev \
        librubberband-dev libshine-dev libsnappy-dev libsoxr-dev libspeex-dev libsrt-openssl-dev libssh-dev \
        libtbb-dev libtheora-dev libtool libtwolame-dev libudev-dev libva-dev libvdpau-dev \
        libvidstab-dev libvorbis-dev libvpl-dev libvpx-dev libwebp-dev \
        libwayland-dev libwayland-egl-backend-dev libx11-dev libx11-xcb-dev \
        libx264-dev libx265-dev libxcb-dri3-dev libxcb-present-dev libxcursor-dev libxext-dev libxfixes-dev \
        libxml2-dev libxvidcore-dev libzimg-dev libzmq3-dev libzvbi-dev ocl-icd-opencl-dev \
        libxi-dev libxinerama-dev libxrandr-dev libxrender-dev meson nasm ninja-build patch patchelf perl \
        libssl-dev libxxhash-dev pkg-config python3 tar xz-utils yasm zlib1g-dev && \
    rm -rf /var/lib/apt/lists/*

COPY ./docker/thirdparty/manifest.env \
     ./docker/thirdparty/build.sh \
     ./docker/thirdparty/build-ffmpeg8.sh \
     ./docker/thirdparty/ffmpeg8-amd.sh \
     ./docker/thirdparty/build-intel-media-stack.sh \
     ./docker/thirdparty/collect-license-manifest.py \
     /build/docker/thirdparty/
COPY ./docker/thirdparty/patches/amf-1.4.36-display-capture-c.patch \
     ./docker/thirdparty/patches/intel-libva-standalone.patch \
     ./docker/thirdparty/patches/intel-media-driver-vpp-deinterlace-crash-fix.patch \
     ./docker/thirdparty/patches/intel-onevpl-gpu-rt-vpp-deinterlace-hang-fix.patch \
     /build/docker/thirdparty/patches/
COPY ./thirdparty-src/tsreadex/ /build/thirdparty-src/tsreadex/

RUN --mount=type=cache,id=konomitv-bs4k-thirdparty-downloads,target=/build/downloads \
    --mount=type=cache,id=konomitv-bs4k-thirdparty-ccache,target=/root/.cache/ccache \
    --mount=type=cache,id=konomitv-bs4k-thirdparty-go-build,target=/root/.cache/go-build \
    --mount=type=cache,id=konomitv-bs4k-thirdparty-go-mod,target=/root/go/pkg/mod \
    chmod +x /build/docker/thirdparty/*.sh && \
    ccache --max-size=20G && \
    ccache --zero-stats && \
    NONFREE="${NONFREE}" /build/docker/thirdparty/build.sh && \
    ccache --show-stats

# CM 解析ランタイムは再生用 FFmpeg と分離し、Amatsukaze 本体を含めず固定構築する。
COPY ./docker/thirdparty/build-cm-analysis.sh /build/docker/thirdparty/build-cm-analysis.sh
COPY ./docker/thirdparty/patches/ffms2-hardware-decoding.patch \
     ./docker/thirdparty/patches/chapter-exe-initialize-avisynth.patch \
     ./docker/thirdparty/patches/logoframe-error-lifetime.patch \
     ./docker/thirdparty/patches/logoframe-parallel-scan.patch \
     ./docker/thirdparty/patches/logoframe-native-luma.patch \
     ./docker/thirdparty/patches/logoframe-high-bit-rgb-fallback.patch \
     /build/docker/thirdparty/patches/
RUN --mount=type=cache,id=konomitv-bs4k-thirdparty-downloads,target=/build/downloads \
    --mount=type=cache,id=konomitv-bs4k-thirdparty-ccache,target=/root/.cache/ccache \
    chmod +x /build/docker/thirdparty/build-cm-analysis.sh && \
    /build/docker/thirdparty/build-cm-analysis.sh && \
    python3 /build/docker/thirdparty/collect-license-manifest.py \
        --stage 'Third-Party Builder Dependencies' \
        --dpkg --dpkg-prefix cuda- --dpkg-exclude cuda-keyring \
        --root /usr/share/vpl/licensing \
        --root /opt/thirdparty \
        --root /build/sources/intel-media-stack \
        --go-module-cache /root/go/pkg/mod \
        --output /tmp/BUILDER_THIRD_PARTY_LICENSES.md

COPY ./docker/thirdparty/generate-license-document.py \
     ./docker/thirdparty/license-manifest.env \
     /build/docker/thirdparty/
COPY ./THIRD_PARTY_LICENSES.md /build/THIRD_PARTY_LICENSES.md
RUN python3 /build/docker/thirdparty/generate-license-document.py --output /tmp/THIRD_PARTY_LICENSES.md && \
    cmp /build/THIRD_PARTY_LICENSES.md /tmp/THIRD_PARTY_LICENSES.md

COPY ./docker/thirdparty/verify.sh \
     ./docker/thirdparty/generate-cm-smoke-fixture.py \
     /build/docker/thirdparty/
RUN chmod +x /build/docker/thirdparty/verify.sh && \
    NONFREE="${NONFREE}" /build/docker/thirdparty/verify.sh /opt/thirdparty

# --------------------------------------------------------------------------------------------------------------
# クライアントをビルドするステージ
# --------------------------------------------------------------------------------------------------------------

FROM node:20.16.0 AS client-builder

WORKDIR /code/client/
COPY ./client/package.json ./client/yarn.lock /code/client/
RUN yarn install --frozen-lockfile
COPY ./client/ /code/client/
RUN yarn build && \
    node scripts/generate-license-document.mjs /tmp/CLIENT_THIRD_PARTY_LICENSES.md

# --------------------------------------------------------------------------------------------------------------
# KonomiTV-BS4K の実行ステージ (Linux amd64 専用)
# --------------------------------------------------------------------------------------------------------------

FROM ubuntu:22.04@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982

ARG CUDA_VERSION=12.4
ARG NONFREE=true
LABEL cc.konomi.konomitv-bs4k.cuda-version="${CUDA_VERSION}" \
      cc.konomi.konomitv-bs4k.nonfree="${NONFREE}"
ENV TZ=Asia/Tokyo
ENV DEBIAN_FRONTEND=noninteractive

RUN case "${CUDA_VERSION}" in \
        '12.4'|'12.8') ;; \
        *) echo 'CUDA_VERSION must be either 12.4 or 12.8.' >&2; exit 1 ;; \
    esac && \
    case "${NONFREE}" in \
        'true'|'false') ;; \
        *) echo 'NONFREE must be either true or false.' >&2; exit 1 ;; \
    esac && \
    apt-get update && apt-get install -y --no-install-recommends ca-certificates nala && \
    printf '%s\n' \
        'deb https://ftp.udx.icscoe.jp/Linux/ubuntu/ jammy main restricted universe multiverse' \
        'deb https://ftp.udx.icscoe.jp/Linux/ubuntu/ jammy-updates main restricted universe multiverse' \
        'deb https://ftp.udx.icscoe.jp/Linux/ubuntu/ jammy-backports main restricted universe multiverse' \
        'deb https://ftp.udx.icscoe.jp/Linux/ubuntu/ jammy-security main restricted universe multiverse' \
        'deb https://ftp.riken.jp/Linux/ubuntu/ jammy main restricted universe multiverse' \
        'deb https://ftp.riken.jp/Linux/ubuntu/ jammy-updates main restricted universe multiverse' \
        'deb https://ftp.riken.jp/Linux/ubuntu/ jammy-backports main restricted universe multiverse' \
        'deb https://ftp.riken.jp/Linux/ubuntu/ jammy-security main restricted universe multiverse' \
        'deb https://ftp.tsukuba.wide.ad.jp/Linux/ubuntu/ jammy main restricted universe multiverse' \
        'deb https://ftp.tsukuba.wide.ad.jp/Linux/ubuntu/ jammy-updates main restricted universe multiverse' \
        'deb https://ftp.tsukuba.wide.ad.jp/Linux/ubuntu/ jammy-backports main restricted universe multiverse' \
        'deb https://ftp.tsukuba.wide.ad.jp/Linux/ubuntu/ jammy-security main restricted universe multiverse' \
        > /etc/apt/sources.list && \
    nala update && nala upgrade -y && nala install -y --no-install-recommends curl git gpg tzdata && \
    curl -fsSL https://repositories.intel.com/gpu/intel-graphics.key | gpg --yes --dearmor --output /usr/share/keyrings/intel-graphics-keyring.gpg && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics-keyring.gpg] https://repositories.intel.com/gpu/ubuntu jammy/lts/2523 unified' > /etc/apt/sources.list.d/intel-gpu-jammy.list && \
    if [ "${NONFREE}" = 'true' ]; then \
        curl -fsSL https://repo.radeon.com/rocm/rocm.gpg.key | gpg --yes --dearmor --output /usr/share/keyrings/rocm-keyring.gpg; \
        echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/rocm-keyring.gpg] https://repo.radeon.com/amdgpu/6.4.4/ubuntu jammy main' > /etc/apt/sources.list.d/amdgpu.list; \
        echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/rocm-keyring.gpg] https://repo.radeon.com/amdgpu/6.4.4/ubuntu jammy proprietary' > /etc/apt/sources.list.d/amdgpu-proprietary.list; \
        echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/rocm-keyring.gpg] https://repo.radeon.com/rocm/apt/6.4.4 jammy main' > /etc/apt/sources.list.d/rocm.list; \
    fi && \
    nala update && nala install -y --no-install-recommends \
        fonts-vlgothic intel-opencl-icd \
        libdrm2 libfontconfig1 libfreetype6 libfribidi0 \
        libaom3 libass9 libbluray2 libgnutls30 libgsm1 libmp3lame0 \
        libmysofa1 libopenjp2-7 libopenmpt0 libopus0 librabbitmq4 \
        librubberband2 libshine3 libsnappy1v5 libsoxr0 libspeex1 libsrt1.4-openssl libssh-4 libssl3 \
        libtheora0 libtwolame0 libva-drm2 libva-x11-2 libvdpau1 libvidstab1.1 libvorbis0a libvorbisenc2 \
        libvpl2 libvpx7 libwebp7 libwebpmux3 libx11-xcb1 libx264-163 libx265-199 libxml2 libxvidcore4 \
        libzimg2 libzmq5 libzvbi0 libxxhash0 ocl-icd-libopencl1 && \
    if [ "${NONFREE}" = 'true' ]; then \
        nala install -y --no-install-recommends \
            amf-amdgpu-pro libamdenc-amdgpu-pro libdrm2-amdgpu mesa-amdgpu-va-drivers \
            rocm-opencl-runtime vulkan-amdgpu-pro; \
        test -e /opt/amdgpu/lib/x86_64-linux-gnu/dri/radeonsi_drv_video.so; \
        if ldd /opt/amdgpu/lib/x86_64-linux-gnu/dri/radeonsi_drv_video.so | grep -F 'not found'; then \
            echo 'AMD VAAPI driver has unresolved dynamic dependencies.' >&2; \
            exit 1; \
        fi; \
    else \
        nala install -y --no-install-recommends mesa-va-drivers; \
        if dpkg-query --show --showformat='${binary:Package}\n' 2>/dev/null | \
                grep -Eq '^(amf-amdgpu-pro|libamdenc-amdgpu-pro|libdrm2-amdgpu|mesa-amdgpu-va-drivers|rocm-opencl-runtime|vulkan-amdgpu-pro)(:amd64)?$'; then \
            echo 'AMD proprietary runtime package must not be installed when NONFREE=false.' >&2; \
            exit 1; \
        fi; \
        test ! -e /opt/amdgpu; \
        test ! -e /opt/rocm; \
        test ! -e /etc/apt/sources.list.d/amdgpu.list; \
        test ! -e /etc/apt/sources.list.d/amdgpu-proprietary.list; \
        test ! -e /etc/apt/sources.list.d/rocm.list; \
        if grep -RqsF 'repo.radeon.com' /etc/apt/sources.list /etc/apt/sources.list.d; then \
            echo 'AMD repository must not be configured when NONFREE=false.' >&2; \
            exit 1; \
        fi; \
        test -e /usr/lib/x86_64-linux-gnu/dri/radeonsi_drv_video.so; \
    fi && \
    nala autoremove -y && apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/*

# NVIDIA のライブラリは NVIDIA Container Toolkit から実行時に注入する。
# CUDA/NGC 由来物が完成イメージへ誤って混入した場合はビルドを失敗させる。
RUN test ! -e /NGC-DL-CONTAINER-LICENSE && \
    if dpkg-query --show --showformat='${binary:Package}\n' | grep -Eq '^(cuda|libnpp|libnv|nvidia)'; then \
        echo 'NVIDIA CUDA runtime package must not be included in the final image.' >&2; \
        exit 1; \
    fi

# Linux Mint は InRelease を提供していないため、署名対象の Release をリモート入力にして
# Chromium の更新時に、ブラウザ導入レイヤーが古いキャッシュを再利用しないようにする
ADD https://fastly.linuxmint.io/dists/virginia/Release /tmp/linuxmint-virginia-Release

RUN curl -fsSL https://fastly.linuxmint.io/pool/main/l/linuxmint-keyring/linuxmint-keyring_2022.06.21_all.deb \
        --output /tmp/linuxmint-keyring.deb && \
    echo 'b71be690c543112ea7b65f43e9bbce9a3f17dd5cc784074858f0824154942a99  /tmp/linuxmint-keyring.deb' | sha256sum --check - && \
    dpkg-deb --extract /tmp/linuxmint-keyring.deb /tmp/linuxmint-keyring && \
    install -m 0644 /tmp/linuxmint-keyring/etc/apt/trusted.gpg.d/linuxmint-keyring.gpg \
        /usr/share/keyrings/linuxmint-archive-keyring.gpg && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/linuxmint-archive-keyring.gpg] https://fastly.linuxmint.io virginia upstream' \
        > /etc/apt/sources.list.d/linuxmint-virginia.list && \
    printf '%s\n' \
        'Package: *' \
        'Pin: release o=linuxmint' \
        'Pin-Priority: -1' \
        '' \
        'Package: chromium' \
        'Pin: release o=linuxmint,n=virginia,c=upstream' \
        'Pin-Priority: 1001' \
        > /etc/apt/preferences.d/linuxmint-chromium && \
    nala update && nala install -y --no-install-recommends chromium libasound2 && \
    chromium_candidate="$(apt-cache policy chromium | awk '/Candidate:/ { print $2; exit }')" && \
    chromium_installed="$(dpkg-query --showformat='${Version}' --show chromium)" && \
    test "${chromium_candidate}" = "${chromium_installed}" && \
    printf '%s' "${chromium_installed}" | grep -Eq '~linuxmint[0-9]+\+virginia$' && \
    apt-cache policy chromium | grep -F 'https://fastly.linuxmint.io virginia/upstream amd64 Packages' && \
    test "$(command -v chromium)" = '/usr/bin/chromium' && \
    chromium --version && \
    apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/*

WORKDIR /code/server/
COPY --from=thirdparty-builder /opt/thirdparty/ /code/server/thirdparty/
RUN test ! -e /code/server/thirdparty/Amatsukaze && \
    test -x /code/server/thirdparty/CMAnalysis/chapter_exe && \
    test -x /code/server/thirdparty/CMAnalysis/ffmsindex && \
    test -x /code/server/thirdparty/CMAnalysis/logoframe && \
    test -x /code/server/thirdparty/CMAnalysis/join_logo_scp && \
    test -s /code/server/thirdparty/CMAnalysis/libffms2.so.3 && \
    python3 -m json.tool /code/server/thirdparty/CMAnalysis/Runtime-Manifest.json > /dev/null && \
    grep -Fx 'CONFIG_GPL=0' /code/server/thirdparty/CMAnalysis/FFmpeg-Build-Configuration.txt && \
    bash -n /code/server/thirdparty/FFmpeg8/ffmpeg8-amd.sh && \
    ffmpeg8_version="$(/code/server/thirdparty/FFmpeg8/ffmpeg8.elf -version | sed -n '1p')" && \
    ffmpeg8_amd_version="$(/code/server/thirdparty/FFmpeg8/ffmpeg8-amd.sh -version | sed -n '1p')" && \
    test "${ffmpeg8_amd_version}" = "${ffmpeg8_version}" && \
    if [ "${NONFREE}" = 'true' ]; then \
        /code/server/thirdparty/FFmpeg8/ffmpeg8-amd.sh -hide_banner -filters 2>&1 | grep -Eq '[[:space:]]deinterlace_vaapi[[:space:]]'; \
    fi
COPY ./server/pyproject.toml ./server/poetry.lock ./server/poetry.toml /code/server/
RUN /code/server/thirdparty/Python/bin/python -m poetry env use /code/server/thirdparty/Python/bin/python && \
    /code/server/thirdparty/Python/bin/python -m poetry install --only main --no-root

COPY ./server/ /code/server/
COPY --from=client-builder /code/client/dist/ /code/client/dist/
COPY ./config.example.yaml /code/config.example.yaml
COPY ./THIRD_PARTY_LICENSES.md /tmp/BASE_THIRD_PARTY_LICENSES.md
COPY ./docker/thirdparty/assemble-runtime-license-document.py /tmp/assemble-runtime-license-document.py
COPY ./docker/thirdparty/collect-license-manifest.py /tmp/collect-license-manifest.py
COPY ./docker/thirdparty/generate-chromium-license-document.py /tmp/generate-chromium-license-document.py
COPY ./docker/thirdparty/licenses/chromium-LICENSE /tmp/chromium-LICENSE
# grapheme 0.6.0 の wheel/sdist は LICENSE を欠くため、公開時コミット
# 7350dfcc1a75a8e347f38ed9e38cb9f9fa928ce0 の上流 LICENSE
# (https://github.com/alvinlindstam/grapheme/blob/7350dfcc1a75a8e347f38ed9e38cb9f9fa928ce0/LICENSE) を固定して補う。
COPY ./docker/thirdparty/licenses/grapheme-0.6.0-LICENSE /tmp/grapheme-0.6.0-LICENSE
COPY --from=client-builder /tmp/CLIENT_THIRD_PARTY_LICENSES.md /tmp/CLIENT_THIRD_PARTY_LICENSES.md
COPY --from=thirdparty-builder /tmp/BUILDER_THIRD_PARTY_LICENSES.md /tmp/BUILDER_THIRD_PARTY_LICENSES.md
RUN if [ "${NONFREE}" = 'true' ]; then nonfree_license_option='--include-nonfree-runtime'; else nonfree_license_option=''; fi && \
    printf '%s  %s\n' \
        '8c09b6ef3ef6bf62858af66af73138b5a234231266c4736009d27233681a5dcf' \
        '/tmp/grapheme-0.6.0-LICENSE' | sha256sum --check --strict - && \
    chromium_version="$(dpkg-query --showformat='${Version}' --show chromium)" && \
    python3 /tmp/generate-chromium-license-document.py \
        --chromium /usr/bin/chromium \
        --package-version "${chromium_version}" \
        --chromium-license /tmp/chromium-LICENSE \
        --package-copyright /usr/share/doc/chromium/copyright \
        --output /code/CHROMIUM_THIRD_PARTY_LICENSES.md && \
    python3 /tmp/collect-license-manifest.py \
        --stage 'Final Runtime Dependencies' --dpkg --dpkg-exclude chromium \
        --root /usr/share/vpl/licensing \
        --root /opt/rocm \
        --python-root /code/server/.venv \
        --python-root /code/server/thirdparty/Python \
        --python-license-override 'grapheme==0.6.0=/tmp/grapheme-0.6.0-LICENSE' \
        --root /code/server/thirdparty/Python \
        --output /tmp/RUNTIME_THIRD_PARTY_LICENSES.md && \
    python3 /tmp/assemble-runtime-license-document.py \
        --base /tmp/BASE_THIRD_PARTY_LICENSES.md \
        --client /tmp/CLIENT_THIRD_PARTY_LICENSES.md \
        --manifest /tmp/BUILDER_THIRD_PARTY_LICENSES.md \
        --manifest /tmp/RUNTIME_THIRD_PARTY_LICENSES.md \
        --cuda-version "${CUDA_VERSION}" \
        ${nonfree_license_option} \
        --output /code/THIRD_PARTY_LICENSES.md && \
    if [ "${NONFREE}" = 'false' ]; then \
        grep -Eq '^#### mesa-va-drivers(:amd64)? ' /code/THIRD_PARTY_LICENSES.md; \
        if grep -Eq 'NONFREE_RUNTIME_WARNING|amf-amdgpu-pro|libamdenc-amdgpu-pro|mesa-amdgpu-va-drivers|vulkan-amdgpu-pro' \
                /code/THIRD_PARTY_LICENSES.md; then \
            echo 'NONFREE=false license document contains non-free runtime metadata.' >&2; \
            exit 1; \
        fi; \
    fi && \
    rm /tmp/BASE_THIRD_PARTY_LICENSES.md /tmp/CLIENT_THIRD_PARTY_LICENSES.md \
        /tmp/BUILDER_THIRD_PARTY_LICENSES.md /tmp/RUNTIME_THIRD_PARTY_LICENSES.md \
        /tmp/assemble-runtime-license-document.py /tmp/collect-license-manifest.py \
        /tmp/generate-chromium-license-document.py /tmp/chromium-LICENSE \
        /tmp/grapheme-0.6.0-LICENSE

ENTRYPOINT ["/code/server/.venv/bin/python", "KonomiTV.py"]
