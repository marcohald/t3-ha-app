# Nightly variant of the T3 Code add-on.
#
# Built in CI with the repository root as the build context and this file as
# the Dockerfile:
#   docker build -f t3-ha-app-nightly/Dockerfile .
# All COPY paths are therefore relative to the repository root, so both
# add-ons share exactly one copy of the application files.
ARG BUILD_FROM=ubuntu:22.04
FROM ${BUILD_FROM}

ARG BUILD_ARCH
ARG BUILD_VERSION=0.0.0
ARG GH_VERSION=2.62.0
# Exact nightly version (tag without the leading v), resolved by CI. Passing
# it avoids an unauthenticated GitHub API lookup during the image build, which
# is otherwise subject to rate limiting (HTTP 403).
ARG T3_VERSION=

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="amd64|aarch64" \
    org.opencontainers.image.title="T3 Code (Nightly)" \
    org.opencontainers.image.description="Nightly T3 Code CLI sharing its data with the stable T3 Code add-on" \
    org.opencontainers.image.source="https://github.com/marcohald/t3-ha-app" \
    org.opencontainers.image.licenses="MIT"

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/data \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    LANGUAGE=en_US:en \
    T3CODE_HOME=/data/.t3 \
    T3_ADDON_VERSION="${BUILD_VERSION}" \
    T3_CHANNEL=nightly \
    T3_SKIP_DATA_INSTALL=1 \
    API_PORT=62906 \
    TTYD_PORT=62905 \
    NGINX_PORT=62901 \
    T3_PORT=3774 \
    PATH="/data/tools/bin:/data/.local/bin:/usr/local/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    bash \
    ttyd \
    tmux \
    jq \
    git \
    nano \
    nginx \
    python3 \
    python3-pip \
    python3-websockets \
    nodejs \
    tar \
    procps \
    psmisc \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /var/log/*

RUN case "${BUILD_ARCH}" in \
        amd64)   GH_ARCH="amd64" ;; \
        aarch64) GH_ARCH="arm64" ;; \
        *) GH_ARCH="amd64" ;; \
    esac && \
    curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${GH_ARCH}.tar.gz" | \
    tar -xz -C /usr/local/bin --strip-components=2 "gh_${GH_VERSION}_linux_${GH_ARCH}/bin/gh" && \
    chmod +x /usr/local/bin/gh && \
    ln -sf /usr/local/bin/gh /usr/bin/gh

RUN case "${BUILD_ARCH}" in \
        amd64)   CLAUDE_PLATFORM="linux-x64" ;; \
        aarch64) CLAUDE_PLATFORM="linux-arm64" ;; \
        *) CLAUDE_PLATFORM="linux-x64" ;; \
    esac && \
    CLAUDE_VER=$(curl -fsSL "https://downloads.claude.ai/claude-code-releases/latest") && \
    curl -fsSL "https://downloads.claude.ai/claude-code-releases/${CLAUDE_VER}/${CLAUDE_PLATFORM}/claude" -o /usr/local/bin/claude && \
    chmod +x /usr/local/bin/claude && \
    ln -sf /usr/local/bin/claude /usr/bin/claude && \
    /usr/local/bin/claude --version

COPY opencode-version.txt /opt/t3code/opencode-version.txt
RUN set -eu; \
    case "${BUILD_ARCH}" in \
        amd64)   OC_ARCH="x64" ;; \
        aarch64) OC_ARCH="arm64" ;; \
        *)       OC_ARCH="x64" ;; \
    esac; \
    oc_version="$(cat /opt/t3code/opencode-version.txt)"; \
    curl -fsSL -o /tmp/opencode.tar.gz \
        "https://github.com/anomalyco/opencode/releases/download/v${oc_version}/opencode-linux-${OC_ARCH}.tar.gz"; \
    tar -xz -C /usr/local/bin -f /tmp/opencode.tar.gz opencode; \
    chmod +x /usr/local/bin/opencode; \
    ln -sf /usr/local/bin/opencode /usr/bin/opencode; \
    rm -f /tmp/opencode.tar.gz; \
    /usr/local/bin/opencode --version

RUN mkdir -p /opt/t3code/html /homeassistant /var/log/nginx && \
    ln -s /config /homeassistant/config && \
    ln -s /share /homeassistant/share

WORKDIR /homeassistant

# Install the newest T3 Code nightly build. T3_SKIP_DATA_INSTALL=1 at runtime
# keeps this image's binary in charge of this add-on, while T3CODE_HOME still
# points at the shared /data/.t3 state directory.
RUN set -eu; \
    [ -n "${T3_VERSION}" ] || { echo "T3_VERSION build arg is required" >&2; exit 1; }; \
    printf '%s\n' "${T3_VERSION}" > /opt/t3code/t3-version.txt; \
    curl -fsSL https://t3.codes/install.sh | \
        T3CODE_CHANNEL=nightly \
        T3CODE_VERSION="${T3_VERSION}" \
        T3CODE_INSTALL_BIN_DIR=/usr/local/bin \
        T3CODE_HOME=/opt/t3 \
        sh; \
    /usr/local/bin/t3 --version | grep -qi nightly || { echo "not a nightly build" >&2; exit 1; }

COPY nginx.conf /etc/nginx/nginx.conf
COPY upload.py /opt/t3code/upload.py
COPY html/ /opt/t3code/html/
COPY attach.sh /opt/t3code/attach.sh
COPY welcome.sh /opt/t3code/welcome.sh
COPY persist.sh /opt/t3code/persist.sh
COPY skills.sh /opt/t3code/skills.sh
COPY t3-runtime.sh /opt/t3code/t3-runtime.sh
COPY t3-relay.sh /opt/t3code/t3-relay.sh
COPY shared-data.sh /opt/t3code/shared-data.sh
COPY install-extra-packages.sh /opt/t3code/install-extra-packages.sh
COPY opencode-runtime.sh /opt/t3code/opencode-runtime.sh
COPY claude-runtime.sh /opt/t3code/claude-runtime.sh
COPY skill/ /opt/t3code/skill/
COPY tmux.conf /opt/t3code/tmux.conf
COPY tmux.conf /etc/tmux.conf
COPY run.sh /run.sh

RUN chmod +x /opt/t3code/attach.sh /opt/t3code/welcome.sh /opt/t3code/persist.sh \
        /opt/t3code/skills.sh /opt/t3code/t3-runtime.sh /opt/t3code/t3-relay.sh \
        /opt/t3code/shared-data.sh /opt/t3code/install-extra-packages.sh \
        /opt/t3code/opencode-runtime.sh /opt/t3code/claude-runtime.sh /run.sh \
        /opt/t3code/skill/homeassistant/scripts/*.py

CMD [ "/run.sh" ]
