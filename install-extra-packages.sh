#!/bin/bash
# Reinstall configured Linux packages after every add-on update.
#
# The container filesystem is rebuilt on every add-on update, so anything
# installed with apt disappears. This script runs from run.sh on every start,
# collects the package names from two sources, and installs only those that are
# actually missing in the fresh container. A plain restart is a no-op.
#
#   1. add-on option `extra_packages` (Settings -> Add-ons -> T3 Code ->
#      Configuration) - /data/options.json
#   2. the skill-managed list - /data/.t3/extra-packages.list
#      (see the `linux-packages` skill; appending there is always confirmed
#      with the user first)
set -u

OPTIONS_FILE="${OPTIONS_FILE:-/data/options.json}"
LIST_FILE="${EXTRA_PACKAGES_LIST:-/data/.t3/extra-packages.list}"
LOG="${EXTRA_PACKAGES_LOG:-/data/.t3/extra-packages.log}"

log() { echo "[Extra Packages] $*" >&2; }

valid_name() {
    [[ "$1" =~ ^[a-zA-Z0-9][a-zA-Z0-9+.-]*$ ]]
}

# Read both sources, strip comments/whitespace, validate, and de-duplicate.
desired="$(
    {
        jq -r '.extra_packages[]? // empty' "$OPTIONS_FILE" 2>/dev/null || true
        [ -f "$LIST_FILE" ] && grep -vE '^[[:space:]]*(#|$)' "$LIST_FILE" 2>/dev/null || true
    } | tr -d '\r' | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' |
      grep -v '^$' | sort -u
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
[ -n "$desired" ] || exit 0

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
