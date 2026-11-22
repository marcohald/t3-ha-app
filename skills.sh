#!/bin/bash
# Install all bundled skills into every T3 agent skill root.
#
#   skills.sh              install the copies bundled in the image (default)
#   skills.sh --github     fetch the newest upstream HA skill first, then install
#
# Skills ship in the image at /opt/t3code/skill/<name> and are copied to $HOME
# (/data), which survives add-on updates. Each provider root links back to the
# single installed copy, so there is exactly one place to update.
set -e

BUNDLED_ROOT="/opt/t3code/skill"
SKILL_ROOT="/data/.claude/skills"
HOME_DIR="${HOME:-/data}"
UPSTREAM_REPO="${UPSTREAM_REPO:-https://github.com/marcohald/ha-agent-skill.git}"
USE_GITHUB=0

case "${1:-}" in
    --github|--update) USE_GITHUB=1 ;;
    "" ) ;;
    *) echo "[Skills] Unknown option: $1" >&2; exit 2 ;;
esac

WORK=""
cleanup() {
    # Keep the exit status of the script: a bare `[ -n ] && rm` here would
    # return 1 when WORK is empty and turn a successful install into a failure.
    if [ -n "${WORK}" ]; then
        rm -rf "${WORK}"
    fi
}
trap cleanup EXIT

# Optionally fetch the newest upstream revision of the HA skill only.
HA_SOURCE="${BUNDLED_ROOT}/homeassistant"
if [ "${USE_GITHUB}" = "1" ]; then
    WORK="$(mktemp -d)"
    if git clone --quiet --depth 1 "${UPSTREAM_REPO}" "${WORK}/upstream" 2>/dev/null; then
        mkdir -p "${WORK}/skill"
        cp "${WORK}/upstream/SKILL.md" "${WORK}/upstream/README.md" "${WORK}/skill/" 2>/dev/null || true
        cp -r "${WORK}/upstream/scripts" "${WORK}/upstream/references" "${WORK}/skill/" 2>/dev/null || true
        if [ -f "${WORK}/skill/SKILL.md" ]; then
            HA_SOURCE="${WORK}/skill"
            echo "[Skills] Fetched the latest skill from ${UPSTREAM_REPO}"
            if command -v git >/dev/null 2>&1; then
                echo "[Skills] Upstream revision: $(git -C "${WORK}/upstream" rev-parse HEAD)"
            fi
        else
            echo "[Skills] Upstream checkout looks incomplete; using the bundled copy." >&2
        fi
    else
        echo "[Skills] Could not reach ${UPSTREAM_REPO}; using the bundled copy." >&2
    fi
fi

mkdir -p "${SKILL_ROOT}"
INSTALLED=""

for candidate in "${BUNDLED_ROOT}"/*; do
    [ -d "${candidate}" ] || continue
    name="$(basename "${candidate}")"
    [ -f "${candidate}/SKILL.md" ] || continue

    src="${candidate}"
    [ "${name}" = "homeassistant" ] && src="${HA_SOURCE}"
    [ -f "${src}/SKILL.md" ] || { echo "[Skills] No SKILL.md for ${name}; skipping." >&2; continue; }

    # Copy (not link) so an agent writing inside the skill can never touch the
    # read-only image layer, and a rebuild can never leave a dangling link.
    rm -rf "${SKILL_ROOT}/${name}"
    cp -r "${src}" "${SKILL_ROOT}/${name}"
    chmod +x "${SKILL_ROOT}/${name}"/scripts/*.py 2>/dev/null || true
    INSTALLED="${INSTALLED} ${name}"
done

if [ -z "${INSTALLED}" ]; then
    echo "[Skills] No skills found under ${BUNDLED_ROOT}." >&2
    exit 1
fi

# Other providers read their own roots. Link each installed skill there.
for root in \
    "${HOME_DIR}/.codex/skills" \
    "${HOME_DIR}/.config/opencode/skills" \
    "${HOME_DIR}/.agents/skills" \
    "${HOME_DIR}/.claw/skills" \
    "${HOME_DIR}/.gemini/config/skills" \
    "${HOME_DIR}/.gemini/antigravity-cli/skills"; do
    [ -d "${root}" ] || mkdir -p "${root}"
    for name in ${INSTALLED}; do
        [ -e "${root}/${name}" ] && continue
        ln -s "${SKILL_ROOT}/${name}" "${root}/${name}" 2>/dev/null || true
    done
done

# The HA skill's WebSocket commands need the `websockets` package; its REST and
# render commands work with the standard library alone.
if ! python3 -c 'import websockets' >/dev/null 2>&1; then
    echo "[Skills] Note: python 'websockets' missing; only HA REST commands will work."
fi

echo "[Skills] Installed skills:${INSTALLED}"
