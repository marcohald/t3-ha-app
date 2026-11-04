ARG BUILD_FROM=debian:bookworm-slim
FROM ${BUILD_FROM}

ARG BUILD_ARCH=amd64
ARG GH_VERSION=2.62.0

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/data \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    LANGUAGE=en_US:en \
    T3CODE_HOME=/data/.t3 \
    PATH="/usr/local/bin:$PATH"

# Install lightweight runtime dependencies (minimal footprint)
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
    chmod +x /usr/local/bin/gh

# Setup application directories and workspace symlinks
RUN mkdir -p /opt/t3code/html /homeassistant && \
    ln -s /config /homeassistant/config && \
    ln -s /share /homeassistant/share

WORKDIR /homeassistant

# Install T3 Code CLI during build for target architecture
RUN curl -fsSL https://t3.codes/install.sh | T3CODE_INSTALL_BIN_DIR=/usr/local/bin T3CODE_HOME=/opt/t3 sh

# Copy configurations, scripts, and web UI files
COPY nginx.conf /etc/nginx/nginx.conf
COPY upload.py /opt/t3code/upload.py
COPY html/ /opt/t3code/html/
COPY attach.sh /opt/t3code/attach.sh
COPY welcome.sh /opt/t3code/welcome.sh
COPY tmux.conf /opt/t3code/tmux.conf
COPY tmux.conf /etc/tmux.conf
COPY run.sh /run.sh

RUN chmod +x /opt/t3code/attach.sh /opt/t3code/welcome.sh /run.sh

CMD [ "/run.sh" ]
