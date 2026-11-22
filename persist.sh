#!/bin/bash
# Persistent (/data) toolchain for T3 Code on Home Assistant.
#
# The add-on container is rebuilt from the image on every update, so anything
# installed outside /data disappears. This script points every common package
# manager at /data, publishes the environment to interactive shells, and writes
# the agent memory files that tell T3's agents where to install software.
set -e

PERSIST_ROOT=/data
TOOLS="${PERSIST_ROOT}/tools"

mkdir -p \
    "${TOOLS}/bin" \
    "${TOOLS}/npm/bin" \
    "${TOOLS}/python/bin" \
    "${TOOLS}/cargo/bin" \
    "${TOOLS}/go/bin" \
    "${TOOLS}/bun/bin" \
    "${TOOLS}/deno/bin" \
    "${TOOLS}/pnpm" \
    "${PERSIST_ROOT}/.local/bin" \
    "${PERSIST_ROOT}/.local/share" \
    "${PERSIST_ROOT}/.cache" \
    "${PERSIST_ROOT}/.config" \
    "${PERSIST_ROOT}/.claude" \
    "${PERSIST_ROOT}/.codex" \
    "${PERSIST_ROOT}/workspace"

# ---------------------------------------------------------------------------
# Environment shared by the entrypoint, tmux/ttyd sessions and the agents.
# ---------------------------------------------------------------------------
cat <<'ENV_EOF' > "${TOOLS}/env.sh"
# Managed by t3-ha-app: keep installed software on the persistent /data volume.
T3_TOOLS="${T3_TOOLS:-/data/tools}"
export T3_TOOLS

# /data/tools/bin first, so a t3 installed into /data wins over the image copy.
if [ "${T3_PERSIST_PATH:-}" != "${T3_TOOLS}" ]; then
    export PATH="${T3_TOOLS}/bin:${T3_TOOLS}/npm/bin:${T3_TOOLS}/python/bin:${T3_TOOLS}/cargo/bin:${T3_TOOLS}/go/bin:${T3_TOOLS}/bun/bin:${T3_TOOLS}/deno/bin:${T3_TOOLS}/pnpm:/data/.local/bin:${PATH}"
    export T3_PERSIST_PATH="${T3_TOOLS}"
fi

# npm / pnpm / yarn
export npm_config_prefix="${T3_TOOLS}/npm"
export NPM_CONFIG_PREFIX="${npm_config_prefix}"
export NODE_PATH="${T3_TOOLS}/npm/lib/node_modules"
export PNPM_HOME="${T3_TOOLS}/pnpm"

# pip
export PIP_USER=1
export PYTHONUSERBASE="${T3_TOOLS}/python"

# Rust
export CARGO_HOME="${T3_TOOLS}/cargo"
export RUSTUP_HOME="${T3_TOOLS}/rustup"

# Go
export GOPATH="${T3_TOOLS}/go"
export GOBIN="${T3_TOOLS}/go/bin"

# bun / deno
export BUN_INSTALL="${T3_TOOLS}/bun"
export DENO_INSTALL="${T3_TOOLS}/deno"

# Caches and per-user data stay on /data as well
export XDG_DATA_HOME="/data/.local/share"
export XDG_CACHE_HOME="/data/.cache"
export XDG_CONFIG_HOME="/data/.config"
export NPM_CONFIG_CACHE="${XDG_CACHE_HOME}/npm"
export PIP_CACHE_DIR="${XDG_CACHE_HOME}/pip"

# Home Assistant credentials for the bundled homeassistant skill. run.sh writes
# this file only when a Supervisor token is available, and callers source this
# file under `set -e`, so the test must not be the script's exit status.
if [ -f /data/tools/ha-env.sh ]; then
    . /data/tools/ha-env.sh
fi
true
ENV_EOF

# Interactive shells (tmux, ttyd) read .bashrc; login shells read .profile.
cat <<'BASHRC_EOF' > "${PERSIST_ROOT}/.bashrc"
# Managed by t3-ha-app.
[ -f /data/tools/env.sh ] && . /data/tools/env.sh
export INPUTRC=/data/.inputrc
BASHRC_EOF

cat <<'PROFILE_EOF' > "${PERSIST_ROOT}/.profile"
# Managed by t3-ha-app.
[ -f /data/tools/env.sh ] && . /data/tools/env.sh
[ -f /data/.bashrc ] && . /data/.bashrc
PROFILE_EOF

cp -f "${PERSIST_ROOT}/.profile" "${PERSIST_ROOT}/.bash_profile"

# ---------------------------------------------------------------------------
# Agent instructions: every T3 chat must know that only /data survives.
# Written as a managed block, so anything the user wrote in these files stays.
# ---------------------------------------------------------------------------
python3 - <<'PY'
import os

BEGIN = "<!-- BEGIN t3-ha-app managed block (do not edit between markers) -->"
END = "<!-- END t3-ha-app managed block -->"

BLOCK = f"""{BEGIN}
## This machine is a Home Assistant add-on container

The container filesystem is rebuilt from the add-on image on every add-on
update. Only `/data` survives. Anything installed in `/usr/local`, `/opt`,
`/etc`, `/home` or `/tmp` is gone after the next update.

### Install software into /data

`PATH`, `npm_config_prefix`, `PIP_USER`/`PYTHONUSERBASE`, `CARGO_HOME`,
`GOPATH`, `BUN_INSTALL` and `DENO_INSTALL` already point at `/data/tools`, so
the usual commands are persistent by default:

- `npm install -g <pkg>` -> `/data/tools/npm`
- `pip install --user <pkg>` -> `/data/tools/python`
- `cargo install <crate>`, `go install <pkg>`, `bun add -g <pkg>`,
  `deno install -g <mod>` -> `/data/tools/...`
- Standalone binaries -> `/data/tools/bin` (already first on `PATH`)
- Repositories and scratch work -> `/data/workspace`

When a tool takes an explicit prefix/install flag, point it at
`/data/tools/<tool>`. Never install into `/usr/local/bin`, `/opt`, `/etc` or
`/home`: those changes disappear on the next add-on update.

`/homeassistant` is the live Home Assistant configuration directory (`/config`)
and `/share` is mapped too. Treat both as user data, not as install locations.

### Home Assistant skill

A skill named `homeassistant` is installed at `~/.claude/skills/homeassistant`
(and linked into the Codex, OpenCode and Antigravity skill roots). Use it
whenever a task touches a running Home Assistant instance: entities,
registries, states, dashboards, long-term statistics, history, templates,
automations, or energy configuration. Confirm access first:

    python3 ~/.claude/skills/homeassistant/scripts/ha_ws.py ping

`HA_URL` and `HA_TOKEN_FILE` are already exported in this environment (the
token is the Supervisor token, rewritten on every start). The scripts are
read-only unless `--allow-write` is passed.

### Linux packages skill

A skill named `linux-packages` is installed in the same roots. When a task needs
an apt package, use it: it installs missing packages immediately and persists
them to the add-on's `extra_packages` option via the Supervisor API (asking the
user first unless they explicitly asked to persist it) so they are reinstalled
after every add-on update. Inspect the current list with:

    python3 ~/.claude/skills/linux-packages/scripts/pkgs.py list
{END}
"""

targets = [
    "/data/.claude/CLAUDE.md",
    "/data/.codex/AGENTS.md",
    "/data/.config/opencode/AGENTS.md",
]

for path in targets:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            current = fh.read()
    except FileNotFoundError:
        current = ""

    if BEGIN in current and END in current:
        head, _, rest = current.partition(BEGIN)
        _, _, tail = rest.partition(END)
        updated = head + BLOCK + tail
    elif current.strip():
        updated = current.rstrip("\n") + "\n\n" + BLOCK
    else:
        updated = BLOCK

    if updated != current:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(updated)
        print(f"[Persist] Wrote agent instructions to {path}")
PY

echo "[Persist] Persistent toolchain ready under ${TOOLS}"
