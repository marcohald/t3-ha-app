#!/bin/bash
# Keep the t3 CLI on the persistent /data volume.
#
# The image ships a pinned t3 build (t3-version.txt, installed to /usr/local/bin
# with its runtime under /opt/t3). This script installs the same version into
# /data/tools/bin, which comes first on PATH, so:
#   - `t3 update` / the add-on UI update the copy that survives add-on updates,
#   - `t3 doctor` and T3's own runtime files stay under /data/.t3,
#   - a new add-on image upgrades the pin, and
#   - a t3 the user installed that is newer than the pin is never downgraded.
#
# Prints the t3 launcher to use on stdout; progress goes to stderr.
set -u

IMAGE_T3="${IMAGE_T3:-/usr/local/bin/t3}"
DATA_BIN_DIR="${DATA_BIN_DIR:-/data/tools/bin}"
DATA_T3="${DATA_BIN_DIR}/t3"
T3_HOME="${T3CODE_HOME:-/data/.t3}"
PIN_FILE="${PIN_FILE:-/opt/t3code/t3-version.txt}"
STATE_FILE="${STATE_FILE:-/data/.t3/t3-installed-version}"
INSTALL_URL="${INSTALL_URL:-https://t3.codes/install.sh}"

log() { echo "[T3 Runtime] $*" >&2; }

pinned_version() {
    [ -s "$PIN_FILE" ] && tr -d '[:space:]' < "$PIN_FILE"
}

binary_version() {
    [ -x "$1" ] || return 0
    "$1" --version 2>/dev/null | sed -n 's/^t3 v\{0,1\}\([0-9][^ ]*\).*/\1/p' | head -n1
}

# version_gt A B -> true when A is newer than B
version_gt() {
    [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" = "$1" ]
}

install_version() {
    version="$1"
    log "Installing t3 ${version} into ${DATA_BIN_DIR} (home: ${T3_HOME})"
    curl -fsSL --connect-timeout 10 --max-time 300 "$INSTALL_URL" \
        | T3CODE_VERSION="$version" \
          T3CODE_HOME="$T3_HOME" \
          T3CODE_INSTALL_BIN_DIR="$DATA_BIN_DIR" \
          sh
}

mkdir -p "$DATA_BIN_DIR" "$T3_HOME/runtime/versions"

# The nightly add-on keeps its own T3 CLI binary from its image so its build
# cannot be replaced by the stable pin in the shared /data volume. All state
# (T3CODE_HOME) is still shared between the two add-ons.
if [ "${T3_SKIP_DATA_INSTALL:-0}" = "1" ]; then
    CURRENT="$(binary_version "$IMAGE_T3")"
    log "Using the image's T3 CLI (${CURRENT:-unknown}); skipping the shared /data launcher."
    printf '%s\n' "$IMAGE_T3"
    exit 0
fi

PINNED="$(pinned_version)"
CURRENT="$(binary_version "$DATA_T3")"

if [ -z "$PINNED" ]; then
    log "No pinned version in ${PIN_FILE}; keeping the installed t3."
elif [ -z "$CURRENT" ] || version_gt "$PINNED" "$CURRENT"; then
    log "Add-on pins t3 ${PINNED} (installed: ${CURRENT:-none})."
    if install_version "$PINNED"; then
        CURRENT="$(binary_version "$DATA_T3")"
    else
        log "Install failed; falling back to the t3 bundled in the image."
    fi
elif version_gt "$CURRENT" "$PINNED"; then
    log "t3 ${CURRENT} in /data is newer than the add-on pin ${PINNED}; keeping it."
fi

if [ -n "$CURRENT" ] && [ -x "$DATA_T3" ]; then
    printf '%s\n' "$CURRENT" > "$STATE_FILE"
    log "Using t3 ${CURRENT} from ${DATA_T3}"
    printf '%s\n' "$DATA_T3"
    exit 0
fi

if [ -x "$IMAGE_T3" ]; then
    log "Using the t3 bundled in the image (${IMAGE_T3})."
    printf '%s\n' "$IMAGE_T3"
    exit 0
fi

log "ERROR: no usable t3 binary found."
exit 1
