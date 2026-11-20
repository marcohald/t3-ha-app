#!/bin/bash
# Keep the Antigravity CLI (agy) on the persistent /data volume.
#
# The image ships a pinned agy binary (antigravity-version.txt, installed to
# /usr/local/bin/agy). This script installs the same pinned version into
# /data/tools/bin, which comes first on PATH, so the CLI survives add-on
# updates just like t3 and claude. agy self-updates in the background at
# runtime; this script only manages the pinned baseline on /data and never
# downgrades a newer binary a user already has.
set -u

IMAGE_AGY="${IMAGE_AGY:-/usr/local/bin/agy}"
DATA_BIN_DIR="${DATA_BIN_DIR:-/data/tools/bin}"
DATA_AGY="${DATA_BIN_DIR}/agy"
PIN_FILE="${PIN_FILE:-/opt/t3code/antigravity-version.txt}"
STATE_FILE="${STATE_FILE:-/data/.t3/antigravity-installed-version}"

log() { echo "[Antigravity Runtime] $*" >&2; }

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
        x86_64|amd64) agy_arch="x64" ;;
        arm64|aarch64) agy_arch="arm64" ;;
        *) agy_arch="x64" ;;
    esac
    tmp="$(mktemp)"
    log "Installing agy ${version} into ${DATA_BIN_DIR}"
    if curl -fsSL --connect-timeout 10 --max-time 300 \
        "https://github.com/google-antigravity/antigravity-cli/releases/download/${version}/agy_cli_linux_${agy_arch}.tar.gz" \
        -o "$tmp" && \
       tar -xz -C "$DATA_BIN_DIR" -f "$tmp" antigravity && \
       mv -f "$DATA_BIN_DIR/antigravity" "$DATA_AGY" && \
       chmod +x "$DATA_AGY"; then
        rm -f "$tmp"
        return 0
    fi
    rm -f "$tmp" "$DATA_BIN_DIR/antigravity" 2>/dev/null || true
    return 1
}

mkdir -p "$DATA_BIN_DIR" "$(dirname "$STATE_FILE")"

PINNED="$(pinned_version)"
CURRENT="$(binary_version "$DATA_AGY")"

if [ -z "$PINNED" ]; then
    log "No pinned version in ${PIN_FILE}; keeping the installed agy."
elif [ -z "$CURRENT" ] || version_gt "$PINNED" "$CURRENT"; then
    log "Add-on pins agy ${PINNED} (installed: ${CURRENT:-none})."
    if install_version "$PINNED"; then
        CURRENT="$(binary_version "$DATA_AGY")"
    else
        log "Install failed; falling back to the agy bundled in the image."
    fi
elif version_gt "$CURRENT" "$PINNED"; then
    log "agy ${CURRENT} in /data is newer than the add-on pin ${PINNED}; keeping it."
fi

if [ -n "$CURRENT" ] && [ -x "$DATA_AGY" ]; then
    printf '%s\n' "$CURRENT" > "$STATE_FILE"
    log "Using agy ${CURRENT} from ${DATA_AGY}"
    printf '%s\n' "$DATA_AGY"
    exit 0
fi

if [ -x "$IMAGE_AGY" ]; then
    log "Using the agy bundled in the image (${IMAGE_AGY})."
    printf '%s\n' "$IMAGE_AGY"
    exit 0
fi

log "WARNING: no usable agy binary found; Antigravity CLI unavailable."
exit 0
