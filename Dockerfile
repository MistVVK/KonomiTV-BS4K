# syntax=docker/dockerfile:1.7

# --------------------------------------------------------------------------------------------------------------
# サードパーティー実行環境を固定構築するステージ
# --------------------------------------------------------------------------------------------------------------

FROM ubuntu:22.04 AS thirdparty-builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
        autoconf automake build-essential ca-certificates ccache cmake curl file git libdrm-dev libffi-dev libglib2.0-dev \
        libnuma-dev libopus-dev libpciaccess-dev libtbb-dev libtool libudev-dev libva-dev \
        libwayland-dev libwayland-egl-backend-dev libx11-dev libx11-xcb-dev \
        libx264-dev libx265-dev libxcb-dri3-dev libxcb-present-dev libxcursor-dev libxext-dev libxfixes-dev \
        libxi-dev libxinerama-dev libxrandr-dev libxrender-dev meson nasm ninja-build patch patchelf perl \
        pkg-config python3 tar xz-utils yasm zlib1g-dev && \
    rm -rf /var/lib/apt/lists/*

COPY ./docker/thirdparty/manifest.env \
     ./docker/thirdparty/build.sh \
     ./docker/thirdparty/build-intel-media-stack.sh \
     /build/docker/thirdparty/
COPY ./docker/thirdparty/patches/ /build/docker/thirdparty/patches/
COPY ./thirdparty-src/tsreadex/ /build/thirdparty-src/tsreadex/

RUN --mount=type=cache,id=konomitv-thirdparty-downloads,target=/build/downloads \
    --mount=type=cache,id=konomitv-thirdparty-ccache,target=/root/.cache/ccache \
    --mount=type=cache,id=konomitv-thirdparty-go-build,target=/root/.cache/go-build \
    --mount=type=cache,id=konomitv-thirdparty-go-mod,target=/root/go/pkg/mod \
    chmod +x /build/docker/thirdparty/*.sh && \
    ccache --max-size=20G && \
    ccache --zero-stats && \
    /build/docker/thirdparty/build.sh && \
    ccache --show-stats

COPY ./docker/thirdparty/generate-license-document.py /build/docker/thirdparty/generate-license-document.py
COPY ./THIRD_PARTY_LICENSES.md /build/THIRD_PARTY_LICENSES.md
RUN python3 /build/docker/thirdparty/generate-license-document.py --output /tmp/THIRD_PARTY_LICENSES.md && \
    cmp /build/THIRD_PARTY_LICENSES.md /tmp/THIRD_PARTY_LICENSES.md

COPY ./docker/thirdparty/verify.sh /build/docker/thirdparty/verify.sh
RUN chmod +x /build/docker/thirdparty/verify.sh && \
    /build/docker/thirdparty/verify.sh /opt/thirdparty

# --------------------------------------------------------------------------------------------------------------
# クライアントをビルドするステージ
# --------------------------------------------------------------------------------------------------------------

FROM node:20.16.0 AS client-builder

WORKDIR /code/client/
COPY ./client/package.json ./client/yarn.lock /code/client/
RUN yarn install --frozen-lockfile
COPY ./client/ /code/client/
RUN yarn build

# --------------------------------------------------------------------------------------------------------------
# KonomiTV の実行ステージ (Linux amd64 専用)
# --------------------------------------------------------------------------------------------------------------

FROM nvidia/cuda:12.4.1-base-ubuntu22.04@sha256:8767a245ed2c481eb245d8f6c625accc3788e1fb8612403d6b4cd4645a4f09c7

ENV TZ=Asia/Tokyo
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-mark hold \
        cuda-compat-12-4 cuda-cudart-12-4 cuda-toolkit-12-4-config-common \
        cuda-toolkit-12-config-common cuda-toolkit-config-common && \
    apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends ca-certificates curl git gpg tzdata && \
    curl -fsSL https://repositories.intel.com/gpu/intel-graphics.key | gpg --yes --dearmor --output /usr/share/keyrings/intel-graphics-keyring.gpg && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics-keyring.gpg] https://repositories.intel.com/gpu/ubuntu jammy unified' > /etc/apt/sources.list.d/intel-gpu-jammy.list && \
    curl -fsSL https://repo.radeon.com/rocm/rocm.gpg.key | gpg --yes --dearmor --output /usr/share/keyrings/rocm-keyring.gpg && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/rocm-keyring.gpg] https://repo.radeon.com/amdgpu/6.4.4/ubuntu jammy main' > /etc/apt/sources.list.d/amdgpu.list && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/rocm-keyring.gpg] https://repo.radeon.com/amdgpu/6.4.4/ubuntu jammy proprietary' > /etc/apt/sources.list.d/amdgpu-proprietary.list && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/rocm-keyring.gpg] https://repo.radeon.com/rocm/apt/6.4.4 jammy main' > /etc/apt/sources.list.d/rocm.list && \
    apt-get update && apt-get install -y --no-install-recommends \
        amf-amdgpu-pro cuda-nvrtc-12-4 fonts-vlgothic intel-opencl-icd \
        libamdenc-amdgpu-pro libdrm2 libdrm2-amdgpu libfontconfig1 libfreetype6 libfribidi0 \
        libnpp-12-4 libopus0 libssl3 libva-drm2 \
        libx11-xcb1 libx264-163 libx265-199 ocl-icd-libopencl1 rocm-opencl-runtime vulkan-amdgpu-pro && \
    apt-get -y autoremove && apt-get -y clean && \
    rm -rf /var/lib/apt/lists/* /tmp/*

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
    apt-get update && apt-get install -y --no-install-recommends chromium libasound2 && \
    chromium_candidate="$(apt-cache policy chromium | awk '/Candidate:/ { print $2; exit }')" && \
    chromium_installed="$(dpkg-query --showformat='${Version}' --show chromium)" && \
    test "${chromium_candidate}" = "${chromium_installed}" && \
    printf '%s' "${chromium_installed}" | grep -Eq '~linuxmint[0-9]+\+virginia$' && \
    apt-cache policy chromium | grep -F 'https://fastly.linuxmint.io virginia/upstream amd64 Packages' && \
    test "$(command -v chromium)" = '/usr/bin/chromium' && \
    chromium --version && \
    apt-get -y clean && \
    rm -rf /var/lib/apt/lists/* /tmp/*

WORKDIR /code/server/
COPY --from=thirdparty-builder /opt/thirdparty/ /code/server/thirdparty/
COPY ./server/pyproject.toml ./server/poetry.lock ./server/poetry.toml /code/server/
RUN /code/server/thirdparty/Python/bin/python -m poetry env use /code/server/thirdparty/Python/bin/python && \
    /code/server/thirdparty/Python/bin/python -m poetry install --only main --no-root

COPY ./server/ /code/server/
COPY --from=client-builder /code/client/dist/ /code/client/dist/
COPY ./config.example.yaml /code/config.example.yaml
COPY ./THIRD_PARTY_LICENSES.md /code/THIRD_PARTY_LICENSES.md

ENTRYPOINT ["/code/server/.venv/bin/python", "KonomiTV.py"]
