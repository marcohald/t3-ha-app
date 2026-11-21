#!/bin/bash
# Share one complete data directory between the stable and nightly add-ons.
#
# Home Assistant gives every add-on its own private /data volume and the
# Supervisor always mounts that private volume at /data, so two add-ons can
# never mount the same /data directly. Both add-ons in this repository map
# /share (read/write) and keep their persistent state in
# /share/t3-ha-app-data instead. Each top-level entry of that directory is
# linked into /data, so every existing script keeps using the familiar /data
# paths while actually reading and writing the single shared copy.
#
# options.json is deliberately left in each add-on's private /data: it holds
# that add-on's own configuration (different ports/channel) and is written by
# the Supervisor itself. Everything else - .t3 state and credentials, the
# persistent toolchain, skills, overlays, relay markers, logs - is shared.
set -u

SHARED_DATA="${SHARED_DATA:-/share/t3-ha-app-data}"
PRIVATE_DATA="${PRIVATE_DATA:-/data}"
LOCK_DIR="${SHARED_DATA}/.migrate.lock"

log() { echo "[Shared Data] $*" >&2; }

mkdir -p "$SHARED_DATA" "$PRIVATE_DATA"

# Only one add-on may migrate at a time.
waited=0
while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    waited=$((waited + 1))
    [ "$waited" -gt 100 ] && { log "Could not acquire migration lock; continuing."; break; }
    sleep 0.2
done
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

# 1. One-time migration: move existing private state into the shared location.
while IFS= read -r -d '' entry; do
    name="$(basename "$entry")"
    [ "$name" = "options.json" ] && continue
    [ -L "$entry" ] && continue

    target="${SHARED_DATA}/${name}"
    if [ -e "$target" ] || [ -L "$target" ]; then
        if [ -d "$target" ] && [ -z "$(ls -A "$target" 2>/dev/null)" ]; then
            # Empty seed created by the sibling add-on: replace it with the
            # real state so nothing is lost.
            log "Replacing empty shared ${name} with existing private state"
            rmdir "$target" 2>/dev/null && mv "$entry" "$target" 2>/dev/null || true
            [ -e "$entry" ] && rm -rf "$entry" 2>/dev/null || true
        elif [ -d "$target" ] && [ -d "$entry" ]; then
            # Both sides have content: merge, shared copy wins on collisions.
            log "Merging ${name} into the shared data directory"
            cp -a "$entry"/. "$target"/ 2>/dev/null || true
            rm -rf "$entry" 2>/dev/null || true
        else
            # Shared copy is authoritative; drop the private leftover.
            rm -rf "$entry" 2>/dev/null || true
        fi
    elif [ -e "$entry" ]; then
        log "Moving ${name} into the shared data directory"
        if ! mv "$entry" "$target" 2>/dev/null; then
            cp -a "$entry" "$target" 2>/dev/null || true
            rm -rf "$entry" 2>/dev/null || true
        fi
    fi
done < <(find "$PRIVATE_DATA" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)

# 2. Seed the standard entries so a brand-new install also uses the shared
#    directory (otherwise a later `mkdir -p /data/.t3` would create private
#    state). This runs only after migration, so existing state is never
#    overwritten by an empty seed.
mkdir -p "$SHARED_DATA/.t3" \
         "$SHARED_DATA/tools" \
         "$SHARED_DATA/workspace" \
         "$SHARED_DATA/.local" \
         "$SHARED_DATA/.claude" \
         "$SHARED_DATA/.config" \
         "$SHARED_DATA/overlay"
touch "$SHARED_DATA/.inputrc" "$SHARED_DATA/t3_serve.log"

# 3. Link every shared entry into /data (also covers entries added by the
#    other add-on after this one started).
while IFS= read -r -d '' entry; do
    name="$(basename "$entry")"
    [ "$name" = ".migrate.lock" ] && continue
    link="${PRIVATE_DATA}/${name}"
    if [ ! -e "$link" ] && [ ! -L "$link" ]; then
        ln -s "$entry" "$link" 2>/dev/null || true
    fi
done < <(find "$SHARED_DATA" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)

log "Using shared data directory ${SHARED_DATA} (options.json stays private)"
