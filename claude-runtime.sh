#!/bin/bash
# Keep Claude Code CLI on the persistent /data volume and available in the image.
#
# The image ships a pinned or latest claude standalone binary in /usr/local/bin.
# This script ensures /data/.local/bin/claude exists and is linked/available,
# falling back to the bundled copy when offline.
set -u

IMAGE_CLAUDE="${IMAGE_CLAUDE:-/usr/local/bin/claude}"
DATA_BIN_DIR="${DATA_BIN_DIR:-/data/.local/bin}"
DATA_CLAUDE="${DATA_BIN_DIR}/claude"

log() { echo "[Claude Runtime] $*" >&2; }

binary_version() {
    [ -x "$1" ] || return 0
    "$1" --version 2>/dev/null | head -n1
}

mkdir -p "$DATA_BIN_DIR" /data/.local/share /data/.claude

CURRENT="$(binary_version "$DATA_CLAUDE")"

if [ -n "$CURRENT" ] && [ -x "$DATA_CLAUDE" ]; then
    log "Using existing claude (${CURRENT}) from ${DATA_CLAUDE}"
    exit 0
fi

if [ -x "$IMAGE_CLAUDE" ]; then
    log "Setting up claude from bundled image (${IMAGE_CLAUDE})."
    ln -sf "$IMAGE_CLAUDE" "$DATA_CLAUDE"
    exit 0
fi

log "Attempting online install via https://claude.ai/install.sh"
if curl -fsSL --connect-timeout 10 --max-time 300 https://claude.ai/install.sh | bash; then
    log "Claude Code successfully installed into /data/.local/bin"
    exit 0
fi

log "WARNING: Claude Code installation failed; CLI not available."
exit 0
