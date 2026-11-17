ARG BUILD_FROM=ubuntu:22.04
FROM ${BUILD_FROM}

# Passed by the Home Assistant builder as amd64 / aarch64. It must be declared
# for the gh/claude install step below to see it, otherwise both images get amd64 binaries.
ARG BUILD_ARCH
ARG BUILD_VERSION=0.0.0
ARG GH_VERSION=2.62.0

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="amd64|aarch64" \
    org.opencontainers.image.title="T3 Code" \
    org.opencontainers.image.description="AI-assisted coding terminal, T3 Code server with mobile companion app pairing, and T3 Connect remote access" \
    org.opencontainers.image.source="https://github.com/marcohald/t3-ha-app" \
    org.opencontainers.image.licenses="MIT"

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/data \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    LANGUAGE=en_US:en \
    T3CODE_HOME=/data/.t3 \
    T3_ADDON_VERSION="${BUILD_VERSION}" \
    PATH="/data/tools/bin:/data/.local/bin:/usr/local/bin:$PATH"

# Install lightweight runtime dependencies (minimal footprint).
# python3-websockets backs the bundled Home Assistant skill.
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
    python3-websockets \
    nodejs \
    tar \
    procps \
    psmisc \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /var/log/*

# Install GitHub CLI (gh) standalone binary for target architecture
RUN case "${BUILD_ARCH}" in \
        amd64)   GH_ARCH="amd64" ;; \
        aarch64) GH_ARCH="arm64" ;; \
        *) GH_ARCH="amd64" ;; \
    esac && \
    curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${GH_ARCH}.tar.gz" | \
    tar -xz -C /usr/local/bin --strip-components=2 "gh_${GH_VERSION}_linux_${GH_ARCH}/bin/gh" && \
    chmod +x /usr/local/bin/gh && \
    ln -sf /usr/local/bin/gh /usr/bin/gh

# Install Claude Code CLI standalone binary for target architecture
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

# Setup application directories and workspace symlinks
RUN mkdir -p /opt/t3code/html /homeassistant /var/log/nginx && \
    ln -s /config /homeassistant/config && \
    ln -s /share /homeassistant/share

WORKDIR /homeassistant

# Install the pinned T3 Code CLI. This copy is the offline fallback: at runtime
# t3-runtime.sh installs the same version under /data so that `t3 update` and
# `t3 doctor` write to the persistent volume instead of a container layer.
COPY t3-version.txt /opt/t3code/t3-version.txt
RUN set -eu; \
    version="$(cat /opt/t3code/t3-version.txt)"; \
    curl -fsSL https://t3.codes/install.sh | \
        T3CODE_VERSION="$version" \
        T3CODE_INSTALL_BIN_DIR=/usr/local/bin \
        T3CODE_HOME=/opt/t3 \
        sh; \
    /usr/local/bin/t3 --version

# Copy configurations, scripts, and web UI files
COPY nginx.conf /etc/nginx/nginx.conf
COPY upload.py /opt/t3code/upload.py
COPY html/ /opt/t3code/html/
COPY attach.sh /opt/t3code/attach.sh
COPY welcome.sh /opt/t3code/welcome.sh
COPY persist.sh /opt/t3code/persist.sh
COPY skills.sh /opt/t3code/skills.sh
COPY t3-runtime.sh /opt/t3code/t3-runtime.sh
COPY claude-runtime.sh /opt/t3code/claude-runtime.sh
COPY skill/homeassistant/ /opt/t3code/skill/homeassistant/
COPY tmux.conf /opt/t3code/tmux.conf
COPY tmux.conf /etc/tmux.conf
COPY run.sh /run.sh

RUN chmod +x /opt/t3code/attach.sh /opt/t3code/welcome.sh /opt/t3code/persist.sh \
        /opt/t3code/skills.sh /opt/t3code/t3-runtime.sh /opt/t3code/claude-runtime.sh /run.sh \
        /opt/t3code/skill/homeassistant/scripts/*.py

CMD [ "/run.sh" ]
