#!/bin/bash
# Install the Home Assistant skill into every T3 agent skill root.
#
#   skills.sh              install the copy bundled in the image (default)
#   skills.sh --github     fetch the newest upstream revision first, then install
#
# The skill ships in the image at /opt/t3code/skill/homeassistant (vendored
# from https://github.com/marcohald/ha-agent-skill). Agents read skills from
# $HOME, which is /data, so the skill is copied there on every start: it is
# present for every provider T3 Code can drive, and a container rebuild never
# leaves an agent without it.
set -e

BUNDLED_SRC="/opt/t3code/skill/homeassistant"
SKILL_ROOT="/data/.claude/skills"
HOME_DIR="${HOME:-/data}"
UPSTREAM_REPO="${UPSTREAM_REPO:-https://github.com/marcohald/ha-agent-skill.git}"
USE_GITHUB=0

case "${1:-}" in
    --github|--update) USE_GITHUB=1 ;;
    "" ) ;;
    *) echo "[Skills] Unknown option: $1" >&2; exit 2 ;;
esac

SRC="${BUNDLED_SRC}"
WORK=""
cleanup() {
    # Keep the exit status of the script: a bare `[ -n ] && rm` here would
    # return 1 when WORK is empty and turn a successful install into a failure.
    if [ -n "${WORK}" ]; then
        rm -rf "${WORK}"
    fi
}
trap cleanup EXIT

if [ "${USE_GITHUB}" = "1" ]; then
    WORK="$(mktemp -d)"
    if git clone --quiet --depth 1 "${UPSTREAM_REPO}" "${WORK}/upstream" 2>/dev/null; then
        # Only SKILL.md, README.md, scripts/ and references/ are installed;
        # upstream's tests/ and eval/ are development material.
        mkdir -p "${WORK}/skill"
        cp "${WORK}/upstream/SKILL.md" "${WORK}/upstream/README.md" "${WORK}/skill/" 2>/dev/null || true
        cp -r "${WORK}/upstream/scripts" "${WORK}/upstream/references" "${WORK}/skill/" 2>/dev/null || true
        if [ -f "${WORK}/skill/SKILL.md" ]; then
            SRC="${WORK}/skill"
            echo "[Skills] Fetched the latest skill from ${UPSTREAM_REPO}"
            if command -v git >/dev/null 2>&1; then
                echo "[Skills] Upstream revision: $(git -C "${WORK}/upstream" rev-parse HEAD)"
            fi
        else
            echo "[Skills] Upstream checkout looks incomplete; using the bundled copy." >&2
            SRC="${BUNDLED_SRC}"
        fi
    else
        echo "[Skills] Could not reach ${UPSTREAM_REPO}; using the bundled copy." >&2
        SRC="${BUNDLED_SRC}"
    fi
fi

if [ ! -f "${SRC}/SKILL.md" ]; then
    echo "[Skills] No skill source available at ${SRC}; skipping." >&2
    exit 1
fi

# Copy (not link) so an agent writing inside the skill can never touch the
# read-only image layer, and a rebuild can never leave a dangling link.
mkdir -p "${SKILL_ROOT}"
rm -rf "${SKILL_ROOT}/homeassistant"
cp -r "${SRC}" "${SKILL_ROOT}/homeassistant"
chmod +x "${SKILL_ROOT}/homeassistant"/scripts/*.py 2>/dev/null || true

# Other providers read their own roots. Link them at the single installed copy
# so there is exactly one place to update. Existing entries are left alone.
for root in \
    "${HOME_DIR}/.codex/skills" \
    "${HOME_DIR}/.config/opencode/skills" \
    "${HOME_DIR}/.agents/skills" \
    "${HOME_DIR}/.claw/skills" \
    "${HOME_DIR}/.gemini/config/skills" \
    "${HOME_DIR}/.gemini/antigravity-cli/skills"; do
    [ -e "${root}/homeassistant" ] && continue
    mkdir -p "${root}"
    ln -s "${SKILL_ROOT}/homeassistant" "${root}/homeassistant" 2>/dev/null || true
done

# The skill's WebSocket commands need the `websockets` package; the REST and
# render commands work with the standard library alone.
if ! python3 -c 'import websockets' >/dev/null 2>&1; then
    echo "[Skills] Note: python 'websockets' missing; only REST commands will work."
fi

echo "[Skills] Installed homeassistant skill at ${SKILL_ROOT}/homeassistant"
