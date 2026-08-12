# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

ARG UBUNTU_2204_IMAGE_SHA256=0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982

# --------------------------------------------------------------------------------------------------------------
# サードパーティー実行環境を固定構築するステージ
# --------------------------------------------------------------------------------------------------------------

FROM ubuntu:22.04@sha256:${UBUNTU_2204_IMAGE_SHA256} AS thirdparty-builder

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

# 同一 UID で動く ACP provider 間の資格情報を OS 強制で分離する Landlock launcher。
# 既存の重い third-party build layer を C source 変更で無効化しないよう、独立した末尾 layer で構築する。
COPY ./docker/acp/KonomiTVBS4KACPSandbox.c /build/docker/acp/KonomiTVBS4KACPSandbox.c
RUN cc -std=c17 -Wall -Wextra -Werror -Wconversion -Wformat=2 -Wshadow -Wstrict-prototypes \
        -fPIE -fstack-protector-strong -D_FORTIFY_SOURCE=2 -O2 \
        -Wl,-z,relro,-z,now -pie \
        /build/docker/acp/KonomiTVBS4KACPSandbox.c \
        -o /opt/konomitv-bs4k-acp-sandbox && \
    chmod 0755 /opt/konomitv-bs4k-acp-sandbox && \
    test "$(stat -c '%a' /opt/konomitv-bs4k-acp-sandbox)" = '755' && \
    file /opt/konomitv-bs4k-acp-sandbox | grep -F 'pie executable'

# --------------------------------------------------------------------------------------------------------------
# KonomiTV-BS4K TS Codec Bridge を完全 commit と source archive SHA-256 から固定構築するステージ
# --------------------------------------------------------------------------------------------------------------

FROM ubuntu:22.04@sha256:${UBUNTU_2204_IMAGE_SHA256} AS tscodecbridge-toolchain

ARG UBUNTU_2204_IMAGE_SHA256
ENV DEBIAN_FRONTEND=noninteractive
ENV KONOMITV_BS4K_TSCODECBRIDGE_EXPECTED_UBUNTU_IMAGE_SHA256=${UBUNTU_2204_IMAGE_SHA256}

# 公開済み commit の完全 SHA と codeload archive SHA-256 が未確定なら、apt や source 取得より前に失敗する。
COPY ./docker/ts-codec-bridge/manifest.env \
     ./docker/ts-codec-bridge/build.sh \
     /build/docker/ts-codec-bridge/
RUN chmod 0755 /build/docker/ts-codec-bridge/build.sh && \
    /build/docker/ts-codec-bridge/build.sh validate-manifest

# SBCL / cl-swank は Jammy 公式 archive の固定 package、SBLint / Mallet は固定 archive だけを使う。
RUN /build/docker/ts-codec-bridge/build.sh prepare

FROM tscodecbridge-toolchain AS tscodecbridge-builder

# Jammy公式toolchainだけで全compile・lint・単体試験を行い、保存実行形式と表示物を固定する。
RUN /build/docker/ts-codec-bridge/build.sh build && \
    test -x /opt/konomitv-bs4k-tscodecbridge-runtime/ts-codec-bridge.elf && \
    test -s /opt/konomitv-bs4k-tscodecbridge-runtime/LICENSE && \
    test -s /opt/konomitv-bs4k-tscodecbridge-runtime/COPYRIGHT-SBCL && \
    test -s /opt/konomitv-bs4k-tscodecbridge-runtime/COPYRIGHT-GLIBC && \
    test -s /opt/konomitv-bs4k-tscodecbridge-runtime/COPYRIGHT-ZLIB && \
    test -s /opt/konomitv-bs4k-tscodecbridge-runtime/Apache-2.0-LICENSE && \
    test -s /opt/konomitv-bs4k-tscodecbridge-runtime/GPL-2-LICENSE && \
    test -s /opt/konomitv-bs4k-tscodecbridge-runtime/LGPL-2.1-LICENSE && \
    test -s /opt/konomitv-bs4k-tscodecbridge-runtime/GFDL-1.3-LICENSE && \
    test -s /opt/konomitv-bs4k-tscodecbridge-runtime/Runtime-Manifest.tsv && \
    test "$(find /opt/konomitv-bs4k-tscodecbridge-runtime -mindepth 1 -maxdepth 1 -type f | wc -l)" -eq 10

# FFmpeg統合試験だけは、全依存を検証済みのthirdparty-builder環境でその成果物を直接用いて実行する。
# Bridge source・toolchain・thirdparty build treeは中間stageに留め、最終imageへは実行形式と表示物だけを移す。
FROM thirdparty-builder AS tscodecbridge-integration

ARG UBUNTU_2204_IMAGE_SHA256
ENV KONOMITV_BS4K_TSCODECBRIDGE_EXPECTED_UBUNTU_IMAGE_SHA256=${UBUNTU_2204_IMAGE_SHA256}

COPY --from=tscodecbridge-builder /build/docker/ts-codec-bridge/ /build/docker/ts-codec-bridge/
COPY --from=tscodecbridge-builder /build/konomitv-bs4k-tscodecbridge/source/ \
    /build/konomitv-bs4k-tscodecbridge/source/
COPY --from=tscodecbridge-builder /opt/konomitv-bs4k-tscodecbridge-runtime/ \
    /opt/konomitv-bs4k-tscodecbridge-runtime/
RUN KONOMITV_BS4K_TSCODECBRIDGE_FFMPEG_ROOT=/opt/thirdparty/FFmpeg8 \
        TSREADEX_BINARY=/opt/thirdparty/tsreadex/tsreadex.elf \
        /build/docker/ts-codec-bridge/build.sh test-ffmpeg-integration

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
# 録画シリーズ ACP CLI を資格情報なしで固定導入するステージ
# --------------------------------------------------------------------------------------------------------------

FROM node:20.16.0 AS acp-builder

# package.json / package-lock.json だけを先に COPY し、dependency layer のキャッシュ境界を明確にする。
# npm の version / integrity の正本は lockfile。manifest.env は展開後 binary と直接依存 version 表示用。
WORKDIR /opt/konomitv-bs4k-acp
COPY ./docker/acp/package.json ./docker/acp/package-lock.json /opt/konomitv-bs4k-acp/
COPY ./docker/acp/manifest.env /build/docker/acp/manifest.env
COPY ./docker/acp/generate-license-document.mjs ./docker/acp/normalize-acp-web-telemetry.mjs \
    ./docker/acp/assemble-acp-license-section.py /build/docker/acp/
RUN set -eu && \
    . /build/docker/acp/manifest.env && \
    # Grok の公式 postinstall は platform package の payload を固定配置へ展開する。
    # builder の root home へ状態を残さず、完成イメージへコピーする配布ディレクトリだけを使う。
    GROK_HOME=/opt/konomitv-bs4k-acp/grok-distribution \
        npm ci --omit=dev --no-audit --no-fund && \
    node /build/docker/acp/normalize-acp-web-telemetry.mjs /opt/konomitv-bs4k-acp && \
    npm ls --depth=0 && \
    test "$(node -p "require('./node_modules/@agentclientprotocol/codex-acp/package.json').version")" = "${CODEX_ACP_VERSION}" && \
    test "$(node -p "require('./node_modules/@openai/codex/package.json').version")" = "${CODEX_CLI_VERSION}" && \
    test "$(node -p "require('./node_modules/${GROK_NPM_PACKAGE}/package.json').version")" = "${GROK_BUILD_VERSION}" && \
    test "$(node -p "require('./node_modules/${GROK_PLATFORM_NPM_PACKAGE}/package.json').version")" = "${GROK_BUILD_VERSION}" && \
    node -e 'const fs=require("fs");const path=require("path");const lock=JSON.parse(fs.readFileSync("package-lock.json","utf8"));const packages=lock.packages||{};function walk(dir,rel,found){if(!fs.existsSync(dir))return;for(const entry of fs.readdirSync(dir,{withFileTypes:true})){if(!entry.isDirectory()||entry.name.startsWith("."))continue;const full=path.join(dir,entry.name);const nextRel=rel?rel+"/"+entry.name:entry.name;if(entry.name.startsWith("@")&&!fs.existsSync(path.join(full,"package.json"))){walk(full,nextRel,found);continue;}const pkgJson=path.join(full,"package.json");if(fs.existsSync(pkgJson)){const pkg=JSON.parse(fs.readFileSync(pkgJson,"utf8"));const lockPath="node_modules/"+nextRel;found.set(lockPath,{name:pkg.name||entry.name,version:pkg.version||""});const nested=path.join(full,"node_modules");if(fs.existsSync(nested))walk(nested,nextRel+"/node_modules",found);}}}const installed=new Map();walk("node_modules","",installed);for(const [lockPath,info] of installed.entries()){const locked=packages[lockPath];if(!locked){console.error("installed path missing from lockfile:",lockPath);process.exit(1);}if(locked.version&&info.version&&locked.version!==info.version){console.error("version mismatch",lockPath,locked.version,info.version);process.exit(1);}if(locked.name&&info.name&&locked.name!==info.name){console.error("name mismatch",lockPath,locked.name,info.name);process.exit(1);}}for(const [lockPath,locked] of Object.entries(packages)){if(!lockPath.startsWith("node_modules/"))continue;if(locked.optional)continue;if(!installed.has(lockPath)){console.error("required lock path not installed:",lockPath);process.exit(1);}}console.log("installed package path/name/version match lockfile");' && \
    # CLI --version は終了成功と既知の完全1行出力の双方を要求する（部分一致 grep は禁止）。
    codex_acp_version="$(/opt/konomitv-bs4k-acp/node_modules/.bin/codex-acp --version)" && \
    codex_cli_version="$(/opt/konomitv-bs4k-acp/node_modules/.bin/codex --version)" && \
    grok_version_line="$(/opt/konomitv-bs4k-acp/grok-distribution/bin/grok --version)" && \
    test "${codex_acp_version}" = "@agentclientprotocol/codex-acp ${CODEX_ACP_VERSION}" && \
    test "${codex_cli_version}" = "codex-cli ${CODEX_CLI_VERSION}" && \
    test "${grok_version_line}" = "${GROK_BUILD_VERSION_LINE}" && \
    printf '%s  %s\n' \
        "${GROK_BUILD_LINUX_X86_64_SHA256}" \
        "/opt/konomitv-bs4k-acp/grok-distribution/bin/grok-${GROK_BUILD_VERSION}" \
        | sha256sum --check --strict - && \
    # Node.js binary と LICENSE のバージョン対応を固定する（LICENSE 欠落は build 失敗）。
    test -s /usr/local/LICENSE && \
    node_version="$(node --version)" && \
    test "${node_version}" = "v20.16.0" && \
    cp /usr/local/LICENSE /opt/konomitv-bs4k-acp/NODE-LICENSE && \
    # lockfile と実 tree を照合した ACP npm ライセンス文書へ、Node.js LICENSE を結合する。
    # Grok binary と THIRD_PARTY_NOTICES は同じ integrity 固定 platform package から収集する。
    node /build/docker/acp/generate-license-document.mjs \
        /opt/konomitv-bs4k-acp/NPM_THIRD_PARTY_LICENSES.md \
        /opt/konomitv-bs4k-acp && \
    python3 /build/docker/acp/assemble-acp-license-section.py \
        --npm-document /opt/konomitv-bs4k-acp/NPM_THIRD_PARTY_LICENSES.md \
        --node-license /opt/konomitv-bs4k-acp/NODE-LICENSE \
        --node-version "${node_version}" \
        --output /opt/konomitv-bs4k-acp/ACP_THIRD_PARTY_LICENSES.md && \
    grep -F '## ACP Runtime Dependencies' /opt/konomitv-bs4k-acp/ACP_THIRD_PARTY_LICENSES.md && \
    grep -F '### Node.js 20.16.0' /opt/konomitv-bs4k-acp/ACP_THIRD_PARTY_LICENSES.md && \
    grep -F '### @agentclientprotocol/codex-acp 1.1.7' /opt/konomitv-bs4k-acp/ACP_THIRD_PARTY_LICENSES.md && \
    grep -F '### @openai/codex 0.145.0' /opt/konomitv-bs4k-acp/ACP_THIRD_PARTY_LICENSES.md && \
    grep -F "### @xai-official/grok ${GROK_BUILD_VERSION}" /opt/konomitv-bs4k-acp/ACP_THIRD_PARTY_LICENSES.md && \
    grep -F "### @xai-official/grok-linux-x64 ${GROK_BUILD_VERSION}" \
        /opt/konomitv-bs4k-acp/ACP_THIRD_PARTY_LICENSES.md && \
    npm cache clean --force

# --------------------------------------------------------------------------------------------------------------
# 録画シリーズ AI 用 opencode serve バイナリを固定導入するステージ
# final には SEA 実行バイナリとライセンス断片のみを持ち込み、node_modules は残さない。
# --------------------------------------------------------------------------------------------------------------

FROM node:20.16.0 AS opencode-builder

WORKDIR /opt/konomitv-bs4k-opencode
COPY ./docker/opencode/package.json ./docker/opencode/package-lock.json /opt/konomitv-bs4k-opencode/
COPY ./docker/opencode/manifest.env /build/docker/opencode/manifest.env
COPY ./docker/opencode/assemble-opencode-license-section.py /build/docker/opencode/
RUN set -eu && \
    . /build/docker/opencode/manifest.env && \
    npm ci --omit=dev --no-audit --no-fund && \
    test "$(node -p "require('./node_modules/opencode-ai/package.json').version")" = "${OPENCODE_VERSION}" && \
    test "$(node -p "require('./node_modules/${OPENCODE_PLATFORM_PACKAGE}/package.json').version")" = "${OPENCODE_VERSION}" && \
    OPENCODE_BIN="/opt/konomitv-bs4k-opencode/node_modules/${OPENCODE_PLATFORM_PACKAGE}/bin/opencode" && \
    test -x "${OPENCODE_BIN}" && \
    # SEA バイナリは Node ランタイム不要。--version は完全1行一致のみ許可する。
    opencode_version="$("${OPENCODE_BIN}" --version)" && \
    test "${opencode_version}" = "${OPENCODE_VERSION}" && \
    mkdir -p /opt/konomitv-bs4k-opencode/dist && \
    cp "${OPENCODE_BIN}" /opt/konomitv-bs4k-opencode/dist/opencode && \
    chmod 755 /opt/konomitv-bs4k-opencode/dist/opencode && \
    test -s /opt/konomitv-bs4k-opencode/node_modules/opencode-ai/LICENSE && \
    cp /opt/konomitv-bs4k-opencode/node_modules/opencode-ai/LICENSE \
        /opt/konomitv-bs4k-opencode/dist/LICENSE && \
    python3 /build/docker/opencode/assemble-opencode-license-section.py \
        --license /opt/konomitv-bs4k-opencode/dist/LICENSE \
        --version "${OPENCODE_VERSION}" \
        --platform-package "${OPENCODE_PLATFORM_PACKAGE}" \
        --output /opt/konomitv-bs4k-opencode/dist/OPENCODE_THIRD_PARTY_LICENSES.md && \
    grep -F '## OpenCode Runtime Dependencies' \
        /opt/konomitv-bs4k-opencode/dist/OPENCODE_THIRD_PARTY_LICENSES.md && \
    grep -F "### opencode-ai ${OPENCODE_VERSION}" \
        /opt/konomitv-bs4k-opencode/dist/OPENCODE_THIRD_PARTY_LICENSES.md && \
    grep -F "### ${OPENCODE_PLATFORM_PACKAGE} ${OPENCODE_VERSION}" \
        /opt/konomitv-bs4k-opencode/dist/OPENCODE_THIRD_PARTY_LICENSES.md && \
    # final へ持ち込むのは dist のみ。node_modules 丸ごとは禁止。
    test ! -e /opt/konomitv-bs4k-opencode/dist/node_modules && \
    npm cache clean --force

# --------------------------------------------------------------------------------------------------------------
# KonomiTV-BS4K の実行ステージ (Linux amd64 専用)
# --------------------------------------------------------------------------------------------------------------

FROM ubuntu:22.04@sha256:${UBUNTU_2204_IMAGE_SHA256} AS runtime

ARG CUDA_VERSION=12.4
ARG NONFREE=true
ARG KONOMITV_UID=1000
ARG KONOMITV_GID=1000
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

# ACP adapter / Codex / Gemini / Grok は公式 npm package と lockfile integrity で固定する。
# Google Cloud CLI は完成イメージへコピーせず、Gemini は host HOME の read-only mount にある ADC だけを参照する。
#
# ----- ACP ホスト認証（イメージには含めない）-----
# Dockerfile / イメージ内に auth.json や ADC を焼き込まない。
# Compose が host HOME を /host-home へ read-only mount し、録画シリーズ設定 UI の
# 「検出済み / 未検出」は、コンテナ内の次の標準 path の読取り可否だけを見る。
#
#   Codex:  /host-home/.codex/auth.json
#   Grok:   /host-home/.grok/auth.json
#   Google: /host-home/.config/gcloud/application_default_credentials.json
#
# 各ファイルが存在しなくても Compose は起動でき、存在する provider だけを UI から利用できる。
# Codex / Grok は UI から専用プロファイルへ「取り込み」、Google ADC は mount 読取りのみ。
# OS keyring だけだと host に auth.json が無いことがある → cli_auth_credentials_store = "file"。
COPY --from=acp-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=acp-builder /opt/konomitv-bs4k-acp/ /opt/konomitv-bs4k-acp/
COPY --from=thirdparty-builder /opt/konomitv-bs4k-acp-sandbox \
    /usr/local/libexec/konomitv-bs4k-acp-sandbox
# opencode は SEA バイナリのみ。node_modules は final に持ち込まない。
COPY --from=opencode-builder /opt/konomitv-bs4k-opencode/dist/opencode /usr/local/bin/opencode
COPY --from=opencode-builder /opt/konomitv-bs4k-opencode/dist/LICENSE \
    /usr/local/share/licenses/opencode/LICENSE
COPY --from=opencode-builder /opt/konomitv-bs4k-opencode/dist/OPENCODE_THIRD_PARTY_LICENSES.md \
    /usr/local/share/licenses/opencode/OPENCODE_THIRD_PARTY_LICENSES.md
COPY ./docker/opencode/opencode.json /usr/local/share/konomitv-bs4k-opencode/opencode.json
RUN ln -s /opt/konomitv-bs4k-acp/node_modules/.bin/codex-acp /usr/local/bin/codex-acp && \
    ln -s /opt/konomitv-bs4k-acp/node_modules/.bin/codex /usr/local/bin/codex && \
    ln -s /opt/konomitv-bs4k-acp/grok-distribution/bin/grok /usr/local/bin/grok && \
    codex_acp_version="$(codex-acp --version)" && \
    codex_cli_version="$(codex --version)" && \
    grok_version_line="$(grok --version)" && \
    opencode_version="$(opencode --version)" && \
    test "${codex_acp_version}" = '@agentclientprotocol/codex-acp 1.1.7' && \
    test "${codex_cli_version}" = 'codex-cli 0.145.0' && \
    test "${grok_version_line}" = 'grok 0.2.112 (9bbd559437)' && \
    test "${opencode_version}" = '1.18.13' && \
    test "$(stat -c '%U:%G:%a' /usr/local/libexec/konomitv-bs4k-acp-sandbox)" = 'root:root:755' && \
    test "$(stat -c '%a' /usr/local/bin/opencode)" = '755' && \
    test -s /usr/local/share/licenses/opencode/LICENSE && \
    test -s /usr/local/share/konomitv-bs4k-opencode/opencode.json && \
    # node_modules 丸ごと持ち込み禁止（バイナリ単体のみ）。
    test ! -e /opt/konomitv-bs4k-opencode && \
    if command -v gcloud >/dev/null 2>&1; then \
        echo 'Google Cloud CLI must not be included in the final image.' >&2; \
        exit 1; \
    fi

WORKDIR /code/server/
COPY --from=thirdparty-builder /opt/thirdparty/ /code/server/thirdparty/
RUN test ! -e /code/server/thirdparty/Amatsukaze && \
    test ! -e /code/server/thirdparty/FFmpeg && \
    test ! -e /code/server/thirdparty/QSVEncC && \
    test ! -e /code/server/thirdparty/NVEncC && \
    test ! -e /code/server/thirdparty/VCEEncC && \
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

# Bridge final surface は保存実行形式、ライセンス・著作権表示、固定 runtime manifest だけにする。
COPY --from=tscodecbridge-integration /opt/konomitv-bs4k-tscodecbridge-runtime/ \
    /code/server/thirdparty/KonomiTVBS4KTSCodecBridge/
RUN --mount=type=bind,from=tscodecbridge-builder,source=/opt/konomitv-bs4k-tscodecbridge-runtime-packages,target=/mnt/konomitv-bs4k-tscodecbridge-runtime-packages \
    set -eu && \
    bridge_root='/code/server/thirdparty/KonomiTVBS4KTSCodecBridge' && \
    runtime_packages_root='/mnt/konomitv-bs4k-tscodecbridge-runtime-packages' && \
    expected_files="$(printf '%s\n' \
        Apache-2.0-LICENSE \
        COPYRIGHT-GLIBC \
        COPYRIGHT-SBCL \
        COPYRIGHT-ZLIB \
        GFDL-1.3-LICENSE \
        GPL-2-LICENSE \
        LGPL-2.1-LICENSE \
        LICENSE \
        Runtime-Manifest.tsv \
        ts-codec-bridge.elf | sort)" && \
    actual_files="$(find "${bridge_root}" -mindepth 1 -maxdepth 1 -type f -printf '%f\n' | sort)" && \
    test "${actual_files}" = "${expected_files}" && \
    test -z "$(find "${bridge_root}" -mindepth 1 -maxdepth 1 ! -type f -print -quit)" && \
    test -z "$(find "${bridge_root}" -type f \( \
        -name '*.asd' -o -name '*.fasl' -o -name '*.lisp' -o -name 'Makefile' \
        \) -print -quit)" && \
    test "$(stat -c '%U:%G:%a' "${bridge_root}/ts-codec-bridge.elf")" = 'root:root:755' && \
    test "$(stat -c '%U:%G:%a' "${bridge_root}/Runtime-Manifest.tsv")" = 'root:root:644' && \
    test -s "${bridge_root}/LICENSE" && \
    test -s "${bridge_root}/COPYRIGHT-SBCL" && \
    test -s "${bridge_root}/COPYRIGHT-GLIBC" && \
    test -s "${bridge_root}/COPYRIGHT-ZLIB" && \
    test -s "${bridge_root}/Apache-2.0-LICENSE" && \
    test -s "${bridge_root}/GPL-2-LICENSE" && \
    test -s "${bridge_root}/LGPL-2.1-LICENSE" && \
    test -s "${bridge_root}/GFDL-1.3-LICENSE" && \
    runtime_manifest="${bridge_root}/Runtime-Manifest.tsv" && \
    ! grep -Fq 'PENDING_' "${runtime_manifest}" && \
    build_glibc_version="$(awk -F '\t' '$1 == "BUILD_GLIBC_PACKAGE_VERSION" { print $2 }' "${runtime_manifest}")" && \
    build_glibc_package_sha256="$(awk -F '\t' '$1 == "LIBC6_PACKAGE_ARCHIVE_SHA256" { print $2 }' "${runtime_manifest}")" && \
    build_glibc_package_url="$(awk -F '\t' '$1 == "LIBC6_PACKAGE_ARCHIVE_URL" { print $2 }' "${runtime_manifest}")" && \
    build_glibc_sha256="$(awk -F '\t' '$1 == "BUILD_GLIBC_LIBRARY_SHA256" { print $2 }' "${runtime_manifest}")" && \
    build_zlib_version="$(awk -F '\t' '$1 == "BUILD_ZLIB_PACKAGE_VERSION" { print $2 }' "${runtime_manifest}")" && \
    build_zlib_package_sha256="$(awk -F '\t' '$1 == "ZLIB1G_PACKAGE_ARCHIVE_SHA256" { print $2 }' "${runtime_manifest}")" && \
    build_zlib_package_url="$(awk -F '\t' '$1 == "ZLIB1G_PACKAGE_ARCHIVE_URL" { print $2 }' "${runtime_manifest}")" && \
    build_zlib_sha256="$(awk -F '\t' '$1 == "BUILD_ZLIB_LIBRARY_SHA256" { print $2 }' "${runtime_manifest}")" && \
    test -n "${build_glibc_version}" && \
    printf '%s' "${build_glibc_package_sha256}" | grep -Eq '^[0-9a-f]{64}$' && \
    test "${build_glibc_package_url}" = "https://archive.ubuntu.com/ubuntu/pool/main/g/glibc/libc6_${build_glibc_version}_amd64.deb" && \
    printf '%s' "${build_glibc_sha256}" | grep -Eq '^[0-9a-f]{64}$' && \
    test -n "${build_zlib_version}" && \
    printf '%s' "${build_zlib_package_sha256}" | grep -Eq '^[0-9a-f]{64}$' && \
    test "${build_zlib_package_url}" = "https://archive.ubuntu.com/ubuntu/pool/main/z/zlib/zlib1g_${build_zlib_version#*:}_amd64.deb" && \
    printf '%s' "${build_zlib_sha256}" | grep -Eq '^[0-9a-f]{64}$' && \
    expected_runtime_packages="$(printf '%s\n' libc6-amd64.deb zlib1g-amd64.deb | sort)" && \
    actual_runtime_packages="$(find "${runtime_packages_root}" -mindepth 1 -maxdepth 1 -type f -printf '%f\n' | sort)" && \
    test "${actual_runtime_packages}" = "${expected_runtime_packages}" && \
    test -z "$(find "${runtime_packages_root}" -mindepth 1 -maxdepth 1 ! -type f -print -quit)" && \
    printf '%s  %s\n' "${build_glibc_package_sha256}" "${runtime_packages_root}/libc6-amd64.deb" | \
        sha256sum --check --strict - && \
    printf '%s  %s\n' "${build_zlib_package_sha256}" "${runtime_packages_root}/zlib1g-amd64.deb" | \
        sha256sum --check --strict - && \
    test "$(dpkg-deb --field "${runtime_packages_root}/libc6-amd64.deb" Package)" = 'libc6' && \
    test "$(dpkg-deb --field "${runtime_packages_root}/libc6-amd64.deb" Version)" = "${build_glibc_version}" && \
    test "$(dpkg-deb --field "${runtime_packages_root}/libc6-amd64.deb" Architecture)" = 'amd64' && \
    test "$(dpkg-deb --field "${runtime_packages_root}/zlib1g-amd64.deb" Package)" = 'zlib1g' && \
    test "$(dpkg-deb --field "${runtime_packages_root}/zlib1g-amd64.deb" Version)" = "${build_zlib_version}" && \
    test "$(dpkg-deb --field "${runtime_packages_root}/zlib1g-amd64.deb" Architecture)" = 'amd64' && \
    dpkg --install \
        "${runtime_packages_root}/libc6-amd64.deb" \
        "${runtime_packages_root}/zlib1g-amd64.deb" && \
    test -z "$(dpkg --audit)" && \
    awk -F '\t' ' \
        NF < 2 { invalid = 1 } \
        $1 == "BUILDER_PACKAGE" { \
            package_count += 1; \
            if (NF != 4 || $4 !~ /^https?:\/\/(archive|security)\.ubuntu\.com\/ubuntu /) invalid = 1; \
        } \
        $1 == "COMMON_LICENSE" { \
            common_license_count += 1; \
            if (NF != 4 || $2 !~ /^(Apache-2\.0|GPL-2|LGPL-2\.1|GFDL-1\.3)-LICENSE$/ || \
                length($3) != 64 || $3 !~ /^[0-9a-f]+$/ || \
                $4 !~ /^\/usr\/share\/common-licenses\//) invalid = 1; \
        } \
        END { exit invalid || package_count == 0 || common_license_count != 4 } \
    ' "${runtime_manifest}" && \
    awk -F '\t' '$1 == "COMMON_LICENSE" { print $3 "  " root "/" $2 }' \
        root="${bridge_root}" "${runtime_manifest}" | \
        sha256sum --check --strict - && \
    cli_version="$(awk -F '\t' '$1 == "CLI_VERSION" { print $2 }' "${runtime_manifest}")" && \
    mapping_version="$(awk -F '\t' '$1 == "TS_MAPPING_VERSION" { print $2 }' "${runtime_manifest}")" && \
    executable_sha256="$(awk -F '\t' '$1 == "EXECUTABLE_SHA256" { print $2 }' "${runtime_manifest}")" && \
    test -n "${cli_version}" && \
    test -n "${mapping_version}" && \
    test -n "${executable_sha256}" && \
    test "$("${bridge_root}/ts-codec-bridge.elf" --version)" = "${cli_version}" && \
    test "$("${bridge_root}/ts-codec-bridge.elf" --mapping-version)" = "${mapping_version}" && \
    printf '%s  %s\n' "${executable_sha256}" "${bridge_root}/ts-codec-bridge.elf" | \
        sha256sum --check --strict - && \
    ldd_output="$(ldd "${bridge_root}/ts-codec-bridge.elf")" && \
    printf '%s\n' "${ldd_output}" && \
    ! printf '%s\n' "${ldd_output}" | grep -Fq 'not found' && \
    printf '%s\n' "${ldd_output}" | grep -Fq 'libc.so.6' && \
    printf '%s\n' "${ldd_output}" | grep -Fq 'libz.so.1' && \
    runtime_glibc_path="$(printf '%s\n' "${ldd_output}" | awk '$1 == "libc.so.6" { print $3; exit }')" && \
    runtime_zlib_path="$(printf '%s\n' "${ldd_output}" | awk '$1 == "libz.so.1" { print $3; exit }')" && \
    test -n "${runtime_glibc_path}" && \
    test -n "${runtime_zlib_path}" && \
    test "$(dpkg-query --showformat='${Version}' --show libc6)" = "${build_glibc_version}" && \
    test "$(dpkg-query --showformat='${Version}' --show zlib1g)" = "${build_zlib_version}" && \
    printf '%s  %s\n' "${build_glibc_sha256}" "${runtime_glibc_path}" | \
        sha256sum --check --strict - && \
    printf '%s  %s\n' "${build_zlib_sha256}" "${runtime_zlib_path}" | \
        sha256sum --check --strict - && \
    ! command -v sbcl >/dev/null 2>&1 && \
    ! command -v mallet >/dev/null 2>&1 && \
    ! command -v sblint >/dev/null 2>&1 && \
    test ! -e /usr/lib/sbcl && \
    test ! -e /usr/share/sbcl-source && \
    test ! -e /opt/mallet && \
    test ! -e /opt/sblint && \
    test ! -e /root/.cache/common-lisp && \
    test ! -e /build/konomitv-bs4k-tscodecbridge && \
    test ! -e /opt/konomitv-bs4k-tscodecbridge-toolchain && \
    test ! -e /opt/konomitv-bs4k-tscodecbridge-ffmpeg8

COPY ./server/pyproject.toml ./server/poetry.lock ./server/poetry.toml /code/server/
RUN /code/server/thirdparty/Python/bin/python -m poetry env use /code/server/thirdparty/Python/bin/python && \
    /code/server/thirdparty/Python/bin/python -m poetry install --only main --no-root

# verify stage からはテストを参照できるよう build context に残しつつ、runtime layer には
# production source だけを記録する。bind mount 内のテストを同じ RUN で除去してから layer を確定する。
RUN --mount=type=bind,source=server,target=/mnt/konomitv-bs4k-server-source \
    cp --archive --no-preserve=ownership /mnt/konomitv-bs4k-server-source/. /code/server/ && \
    rm -rf /code/server/tests && \
    test ! -e /code/server/tests
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
COPY --from=acp-builder /opt/konomitv-bs4k-acp/ACP_THIRD_PARTY_LICENSES.md /tmp/ACP_THIRD_PARTY_LICENSES.md
COPY --from=opencode-builder /opt/konomitv-bs4k-opencode/dist/OPENCODE_THIRD_PARTY_LICENSES.md \
    /tmp/OPENCODE_THIRD_PARTY_LICENSES.md
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
        --root /code/server/thirdparty/KonomiTVBS4KTSCodecBridge \
        --output /tmp/RUNTIME_THIRD_PARTY_LICENSES.md && \
    grep -F 'KonomiTVBS4KTSCodecBridge: LICENSE' /tmp/RUNTIME_THIRD_PARTY_LICENSES.md && \
    grep -F 'KonomiTVBS4KTSCodecBridge: COPYRIGHT-SBCL' /tmp/RUNTIME_THIRD_PARTY_LICENSES.md && \
    grep -F 'KonomiTVBS4KTSCodecBridge: COPYRIGHT-GLIBC' /tmp/RUNTIME_THIRD_PARTY_LICENSES.md && \
    grep -F 'KonomiTVBS4KTSCodecBridge: COPYRIGHT-ZLIB' /tmp/RUNTIME_THIRD_PARTY_LICENSES.md && \
    grep -F 'KonomiTVBS4KTSCodecBridge: Apache-2.0-LICENSE' /tmp/RUNTIME_THIRD_PARTY_LICENSES.md && \
    grep -F 'KonomiTVBS4KTSCodecBridge: GPL-2-LICENSE' /tmp/RUNTIME_THIRD_PARTY_LICENSES.md && \
    grep -F 'KonomiTVBS4KTSCodecBridge: LGPL-2.1-LICENSE' /tmp/RUNTIME_THIRD_PARTY_LICENSES.md && \
    grep -F 'KonomiTVBS4KTSCodecBridge: GFDL-1.3-LICENSE' /tmp/RUNTIME_THIRD_PARTY_LICENSES.md && \
    # ACP 配布物（npm tree + Node.js）は独立 H2 として最終文書へ統合する。
    # ACP セクションは npm 全件（公式 Grok platform package を含む）+ Node.js を必須とする。
    test -s /tmp/ACP_THIRD_PARTY_LICENSES.md && \
    grep -F '## ACP Runtime Dependencies' /tmp/ACP_THIRD_PARTY_LICENSES.md && \
    grep -F '### Node.js 20.16.0' /tmp/ACP_THIRD_PARTY_LICENSES.md && \
    grep -F '### @agentclientprotocol/codex-acp 1.1.7' /tmp/ACP_THIRD_PARTY_LICENSES.md && \
    grep -F '### @openai/codex 0.145.0' /tmp/ACP_THIRD_PARTY_LICENSES.md && \
    grep -F '### @xai-official/grok 0.2.112' /tmp/ACP_THIRD_PARTY_LICENSES.md && \
    grep -F '### @xai-official/grok-linux-x64 0.2.112' /tmp/ACP_THIRD_PARTY_LICENSES.md && \
    python3 /tmp/assemble-runtime-license-document.py \
        --base /tmp/BASE_THIRD_PARTY_LICENSES.md \
        --client /tmp/CLIENT_THIRD_PARTY_LICENSES.md \
        --manifest /tmp/BUILDER_THIRD_PARTY_LICENSES.md \
        --manifest /tmp/RUNTIME_THIRD_PARTY_LICENSES.md \
        --manifest /tmp/ACP_THIRD_PARTY_LICENSES.md \
        --manifest /tmp/OPENCODE_THIRD_PARTY_LICENSES.md \
        --cuda-version "${CUDA_VERSION}" \
        ${nonfree_license_option} \
        --output /code/THIRD_PARTY_LICENSES.md && \
    grep -F '## ACP Runtime Dependencies' /code/THIRD_PARTY_LICENSES.md && \
    grep -F '### Node.js 20.16.0' /code/THIRD_PARTY_LICENSES.md && \
    grep -F '### @agentclientprotocol/codex-acp 1.1.7' /code/THIRD_PARTY_LICENSES.md && \
    grep -F '### @openai/codex 0.145.0' /code/THIRD_PARTY_LICENSES.md && \
    grep -F '### @xai-official/grok 0.2.112' /code/THIRD_PARTY_LICENSES.md && \
    grep -F '### @xai-official/grok-linux-x64 0.2.112' /code/THIRD_PARTY_LICENSES.md && \
    grep -F '## OpenCode Runtime Dependencies' /code/THIRD_PARTY_LICENSES.md && \
    grep -F '### opencode-ai 1.18.13' /code/THIRD_PARTY_LICENSES.md && \
    grep -F '### opencode-linux-x64 1.18.13' /code/THIRD_PARTY_LICENSES.md && \
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
        /tmp/ACP_THIRD_PARTY_LICENSES.md /tmp/OPENCODE_THIRD_PARTY_LICENSES.md \
        /tmp/assemble-runtime-license-document.py /tmp/collect-license-manifest.py \
        /tmp/generate-chromium-license-document.py /tmp/chromium-LICENSE \
        /tmp/grapheme-0.6.0-LICENSE

# KonomiTV-BS4K 本体は専用の非 root ユーザーで実行する。
# bind mount する config.yaml・data・logs も、ホスト側で同じ UID/GID が所有している必要がある。
RUN groupadd --gid "${KONOMITV_GID}" konomitv && \
    useradd --uid "${KONOMITV_UID}" --gid "${KONOMITV_GID}" \
        --home-dir /home/konomitv --create-home --shell /usr/sbin/nologin konomitv && \
    install -d -o "${KONOMITV_UID}" -g "${KONOMITV_GID}" -m 0750 \
        /code/server/data /code/server/logs /home/konomitv/.cache && \
    chown -R "${KONOMITV_UID}:${KONOMITV_GID}" /code/server/data /code/server/logs

ENV HOME=/home/konomitv
USER konomitv:konomitv

ENTRYPOINT ["/code/server/.venv/bin/python", "KonomiTV.py"]

# --------------------------------------------------------------------------------------------------------------
# クライアントの非変更型 lint・型検査・単体試験・ライセンス試験を実行するステージ
# --------------------------------------------------------------------------------------------------------------

FROM client-builder AS client-verify

RUN yarn lint:check && \
    yarn typecheck && \
    yarn test && \
    yarn test:licenses && \
    touch /tmp/konomitv-bs4k-client-verify.ok

# --------------------------------------------------------------------------------------------------------------
# サーバーの非変更型 lint・型検査・全 pytest を実行するステージ
# --------------------------------------------------------------------------------------------------------------

FROM runtime AS server-verify

# runtime は非 root のまま維持し、build 中だけ C fixture の構築と開発依存導入に root を使う。
USER root
RUN nala update && \
    apt-get install -y --fix-broken && \
    apt-get install -y --no-install-recommends build-essential && \
    command -v cc >/dev/null && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# pytest には Dockerfile・配布スクリプト・Compose policy も検査するテストがあるため、
# 秘密とホスト状態を .dockerignore で除外した上で、verify stage にだけ必要なリポジトリ面を配置する。
COPY ./Dockerfile ./.dockerignore ./.gitignore ./.env.example ./THIRD_PARTY_LICENSES.md \
     ./compose.yaml \
     ./compose.development.yaml \
     ./compose.intel-amd.yaml \
     ./compose.nvidia.yaml \
     /code/
COPY ./docker/acp/ /code/docker/acp/
COPY ./docker/opencode/ /code/docker/opencode/
COPY ./docker/thirdparty/ /code/docker/thirdparty/
COPY ./docker/ts-codec-bridge/ /code/docker/ts-codec-bridge/
COPY ./server/ /code/server/
# Python 側のライセンス generator 試験も Node.js package tree を検証するため、client-builder を共有する。
COPY --from=client-builder /code/client/ /code/client/

RUN /code/server/thirdparty/Python/bin/python -m poetry install --with dev --no-root && \
    /code/server/thirdparty/Python/bin/python -m poetry run task lint-check && \
    /code/server/thirdparty/Python/bin/python -m poetry run task test && \
    touch /tmp/konomitv-bs4k-server-verify.ok

# --------------------------------------------------------------------------------------------------------------
# サーバー・クライアント双方の検証成功を集約するステージ
# --------------------------------------------------------------------------------------------------------------

FROM server-verify AS verify

COPY --from=client-verify /tmp/konomitv-bs4k-client-verify.ok /tmp/konomitv-bs4k-client-verify.ok
RUN test -f /tmp/konomitv-bs4k-server-verify.ok && \
    test -f /tmp/konomitv-bs4k-client-verify.ok && \
    touch /tmp/konomitv-bs4k-verify.ok

# --------------------------------------------------------------------------------------------------------------
# verify 成功を build 依存関係に持つ、開発者向けの検証済み runtime ステージ
# --------------------------------------------------------------------------------------------------------------

FROM runtime AS verified-runtime

# verify の marker は read-only mount で確認し、完成イメージへテストや開発依存をコピーしない。
RUN --mount=type=bind,from=verify,source=/tmp/konomitv-bs4k-verify.ok,target=/tmp/konomitv-bs4k-verify.ok \
    test -f /tmp/konomitv-bs4k-verify.ok

# target 未指定の既存 Compose build は従来どおり公開用 runtime を生成する。
FROM runtime AS default-runtime
