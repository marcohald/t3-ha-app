#!/bin/bash
# Reinstall configured Linux packages after every add-on update.
#
# The container filesystem is rebuilt on every add-on update, so anything
# installed with apt disappears. This script runs from run.sh on every start,
# reads the configured `extra_packages` list, and installs only those packages
# that are actually missing in the fresh container. A plain restart is a no-op.
#
# The single source of truth is the add-on option `extra_packages` (visible in
# Settings -> Add-ons -> T3 Code -> Configuration). The `linux-packages` skill
# writes to the same option via the Supervisor API (`POST /addons/self/options`),
# so both the UI and the skill stay in sync.
set -u

OPTIONS_FILE="${OPTIONS_FILE:-/data/options.json}"
LOG="${EXTRA_PACKAGES_LOG:-/data/.t3/extra-packages.log}"
SUPERVISOR_URL="${SUPERVISOR_URL:-http://supervisor}"
HA_TOKEN_FILE="${HA_TOKEN_FILE:-/data/.t3/ha.token}"

log() { echo "[Extra Packages] $*" >&2; }

valid_name() {
    [[ "$1" =~ ^[a-zA-Z0-9][a-zA-Z0-9+.-]*$ ]]
}

# Prefer live Supervisor options so API updates made without a restart take
# effect immediately, falling back to /data/options.json if unreachable.
AUTH_TOKEN="${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}"
if [ -z "$AUTH_TOKEN" ] && [ -r "$HA_TOKEN_FILE" ]; then
    AUTH_TOKEN="$(tr -d '\r\n' < "$HA_TOKEN_FILE" 2>/dev/null || true)"
fi

api_packages=""
api_ok=0
if [ -n "$AUTH_TOKEN" ]; then
    api_json="$(curl -fsS --max-time 3 \
        -H "Authorization: Bearer ${AUTH_TOKEN}" \
        "${SUPERVISOR_URL%/}/addons/self/info" 2>/dev/null || true)"
    if [ -n "$api_json" ] && [ "$(printf '%s' "$api_json" | jq -r '.result // empty' 2>/dev/null || true)" = "ok" ]; then
        api_packages="$(printf '%s' "$api_json" | jq -r '.data.options.extra_packages[]? // empty' 2>/dev/null || true)"
        api_ok=1
    fi
fi

if [ "$api_ok" -eq 1 ]; then
    desired_raw="$api_packages"
else
    desired_raw="$(jq -r '.extra_packages[]? // empty' "$OPTIONS_FILE" 2>/dev/null || true)"
fi

# Strip whitespace, validate, and de-duplicate.
desired="$(
    printf '%s\n' "$desired_raw" \
      | tr -d '\r' | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
      | grep -v '^$' | sort -u || true
)"

[ -n "$desired" ] || { log "No extra packages configured."; exit 0; }

clean=""
while IFS= read -r pkg; do
    [ -n "$pkg" ] || continue
    if valid_name "$pkg"; then
        clean="${clean}${pkg}"$'\n'
    else
        log "Ignoring invalid package name: ${pkg}"
    fi
done <<< "$desired"
desired="${clean%$'\n'}"
[ -n "$desired" ] || exit 0;

missing=""
while IFS= read -r pkg; do
    [ -n "$pkg" ] || continue
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
        missing="${missing} ${pkg}"
    fi
done <<< "$desired"

if [ -z "${missing# }" ]; then
    log "All configured packages are already installed."
    exit 0
fi

log "Installing missing packages:${missing}"
export DEBIAN_FRONTEND=noninteractive
{
    echo "$(date -Is) installing:${missing}"
    apt-get update -qq && apt-get install -y --no-install-recommends $missing
} >> "$LOG" 2>&1 || {
    rc=$?
    log "Installation failed (exit ${rc}); see ${LOG}"
    exit 0
}

log "Done."
