#!/bin/bash
# Refresh the vendored copy of marcohald/ha-agent-skill.
#
#   ./skill/update-skill.sh              # newest master
#   ./skill/update-skill.sh --rev <sha>  # pin an exact commit
#
# Only SKILL.md, README.md, scripts/ and references/ are vendored; upstream's
# tests/ and eval/ are development material and stay out of the image.
set -e

REPO=https://github.com/marcohald/ha-agent-skill.git
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${HERE}/homeassistant"

REV=""
if [ "${1:-}" = "--rev" ]; then
    REV="${2:?--rev needs a commit sha}"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone --quiet "$REPO" "$WORK/upstream"
if [ -n "$REV" ]; then
    git -C "$WORK/upstream" checkout --quiet "$REV"
fi

PINNED="$(git -C "$WORK/upstream" rev-parse HEAD)"

rm -rf "$DEST"
mkdir -p "$DEST"
cp "$WORK/upstream/SKILL.md" "$WORK/upstream/README.md" "$DEST/"
cp -r "$WORK/upstream/scripts" "$WORK/upstream/references" "$DEST/"
chmod +x "$DEST"/scripts/*.py

echo "Vendored homeassistant skill at ${PINNED}"
echo
echo "Diff against the working tree:"
git -C "$HERE/.." --no-pager diff --stat -- "skill/homeassistant" || true
echo
echo "Review it (the skill can reach the Home Assistant API), then bump the"
echo "add-on version in config.yaml / Dockerfile / CHANGELOG.md and commit."
