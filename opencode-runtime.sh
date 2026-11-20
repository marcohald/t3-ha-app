#!/bin/bash
# Keep the OpenCode CLI on the persistent /data volume.
#
# The image ships a pinned opencode binary (opencode-version.txt, installed to
# /usr/local/bin/opencode). This script installs the same pinned version into
# /data/tools/bin, which comes first on PATH, so the CLI survives add-on
# updates just like t3 and claude. OpenCode can self-update at runtime; this
# script only manages the pinned baseline on /data and never downgrades a
# newer binary a user already has.
set -u

IMAGE_OPENCODE="${IMAGE_OPENCODE:-/usr/local/bin/opencode}"
DATA_BIN_DIR="${DATA_BIN_DIR:-/data/tools/bin}"
DATA_OPENCODE="${DATA_BIN_DIR}/opencode"
PIN_FILE="${PIN_FILE:-/opt/t3code/opencode-version.txt}"
STATE_FILE="${STATE_FILE:-/data/.t3/opencode-installed-version}"

log() { echo "[OpenCode Runtime] $*" >&2; }

pinned_version() {
    [ -s "$PIN_FILE" ] && tr -d '[:space:]' < "$PIN_FILE"
}

binary_version() {
    [ -x "$1" ] || return 0
    "$1" --version 2>/dev/null | sed -n 's/^v\{0,1\}\([0-9][^ ]*\).*/\1/p' | head -n1
}

# version_gt A B -> true when A is newer than B
version_gt() {
    [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" = "$1" ]
}

install_version() {
    version="$1"
    case "$(uname -m)" in
        x86_64|amd64) oc_arch="x64" ;;
        arm64|aarch64) oc_arch="arm64" ;;
        *) oc_arch="x64" ;;
    esac
    tmp="$(mktemp)"
    log "Installing opencode ${version} into ${DATA_BIN_DIR}"
    if curl -fsSL --connect-timeout 10 --max-time 300 \
        "https://github.com/anomalyco/opencode/releases/download/v${version}/opencode-linux-${oc_arch}.tar.gz" \
        -o "$tmp" && \
       tar -xz -C "$DATA_BIN_DIR" -f "$tmp" opencode && \
       chmod +x "$DATA_OPENCODE"; then
        rm -f "$tmp"
        return 0
    fi
    rm -f "$tmp" "$DATA_BIN_DIR/opencode" 2>/dev/null || true
    return 1
}

mkdir -p "$DATA_BIN_DIR" "$(dirname "$STATE_FILE")"

PINNED="$(pinned_version)"
CURRENT="$(binary_version "$DATA_OPENCODE")"

if [ -z "$PINNED" ]; then
    log "No pinned version in ${PIN_FILE}; keeping the installed opencode."
elif [ -z "$CURRENT" ] || version_gt "$PINNED" "$CURRENT"; then
    log "Add-on pins opencode ${PINNED} (installed: ${CURRENT:-none})."
    if install_version "$PINNED"; then
        CURRENT="$(binary_version "$DATA_OPENCODE")"
    else
        log "Install failed; falling back to the opencode bundled in the image."
    fi
elif version_gt "$CURRENT" "$PINNED"; then
    log "opencode ${CURRENT} in /data is newer than the add-on pin ${PINNED}; keeping it."
fi

if [ -n "$CURRENT" ] && [ -x "$DATA_OPENCODE" ]; then
    printf '%s\n' "$CURRENT" > "$STATE_FILE"
    log "Using opencode ${CURRENT} from ${DATA_OPENCODE}"
    printf '%s\n' "$DATA_OPENCODE"
    exit 0
fi

if [ -x "$IMAGE_OPENCODE" ]; then
    log "Using the opencode bundled in the image (${IMAGE_OPENCODE})."
    printf '%s\n' "$IMAGE_OPENCODE"
    exit 0
fi

log "WARNING: no usable opencode binary found; OpenCode CLI unavailable."
exit 0
