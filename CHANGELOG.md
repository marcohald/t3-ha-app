# Changelog

## 1.1.14 (2026-11-21)

- 🔁 **T3 Connect relay survives add-on updates**: when the relay was connected before an update, a marker on `/data` now triggers automatic restoration at startup — stale relay-client PID state is cleared, `t3 connect` re-installs/relinks the managed relay client, and `t3 serve` is restarted so the environment link is provisioned and the tunnel comes back without manual intervention. Logout clears the marker. Existing installs with a stored credential are detected via the fallback in `t3-relay.sh`.

## 1.1.13 (2026-11-21)

- ⚡ **OpenCode CLI bundled**: native `opencode` binary (pinned to `2.3.2` via `opencode-version.txt`) preinstalled for `amd64`/`aarch64` and mapped persistently to `/data/tools/bin` by `opencode-runtime.sh`, so it survives add-on updates. Replaces the previously bundled Antigravity CLI.
- 🤖 **Automatic OpenCode release tracking**: new `.github/workflows/track-opencode-releases.yaml` checks `anomalyco/opencode` every six hours, bumps the pin and add-on version, and rebuilds/publishes the images through CI — no local build required.
- 📊 **Update check**: `/version_check` now reports the installed OpenCode version, the newest stable v2 release and whether an update is available; the Ingress status view shows it and the CI integration test verifies the installed version matches the pin.
- 🐍 **pip3 in the image**: added `python3-pip` so `pip3 install --user` works out of the box (and lands in `/data/tools/python` via `persist.sh`).

## 1.1.11 (2026-11-17)

- 🧪 **Pre-publish CI integration test**: added container test suite verifying all services (`t3 serve`, `nginx`, `upload.py`), CLI tools and endpoints before building and publishing images.
- 🤖 **Claude Code CLI bundled**: native standalone `claude` binary preinstalled in container and mapped persistently to `/data/.local/bin`.
- 🐛 **Fix `t3 serve` boot failure**: removed deprecated `--auto-bootstrap-project-from-cwd` flag incompatible with T3 Code v0.1.10.
- 📊 **UI & Status Enhancements**: added Claude CLI version to status report and welcome banner.


## 1.1.10 - Automatic T3 Releases, Persistent Toolchain & Bundled HA Skill

- 🤖 **Automatic T3 Code release tracking**: `.github/workflows/track-t3-releases.yaml` checks `pingdotgg/t3code` every six hours for a new stable release, bumps `t3-version.txt` and the add-on version (`1.<T3 minor>.<T3 patch>`), commits, builds and publishes the multi-arch images itself, verifies the GHCR manifest for `amd64`/`aarch64`, and publishes a GitHub release. Failed builds revert the bump automatically.
- 📌 **Single version pin**: `t3-version.txt` is now the one source of truth for the shipped T3 Code version (currently `0.1.10`); the Dockerfile installs exactly that version.
- 💾 **Update-secure installs**: new `persist.sh` points `PATH`, npm, pip, cargo, go, bun, deno and XDG paths at `/data/tools`, so `npm install -g`, `pip install --user`, `cargo install`, … survive add-on updates.
- ♻️ **Persistent T3 CLI**: new `t3-runtime.sh` installs/upgrades `t3` under `/data` (never downgrades), so `t3 update` and `t3 doctor` persist; the image copy is kept as an offline fallback.
- 📝 **Agent instructions**: managed blocks in `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` and `~/.config/opencode/AGENTS.md` tell every T3 chat to install software into `/data` and how to use the Home Assistant skill.
- 🧠 **Bundled Home Assistant skill**: [`ha-agent-skill`](https://github.com/marcohald/ha-agent-skill) vendored in `skill/homeassistant/` (pinned upstream commit), installed to `/data/.claude/skills/homeassistant` on every start and linked into the Codex, OpenCode, fx/claw and Antigravity skill roots. `HA_URL`/`HA_TOKEN` are exported for it.
- 🔄 **Skill updates**: new `skill/update-skill.sh` re-vendors the skill from upstream; the UI gains **HA-Skill aktualisieren** and **T3 CLI aktualisieren** actions, backed by the new `/t3_skill` API endpoint (`skills.sh --github`).
- 🩹 **Build fix**: the Dockerfile now declares `BUILD_ARCH` (the old `BUILD_ARmd64` typo meant the arm64 image shipped an amd64 `gh` binary), and the version label is fed from the builder's `BUILD_VERSION`.
- ℹ️ **Richer version check**: `/version_check` now reports the installed and pinned T3 version, the add-on version, the latest upstream release and the skill status; the version check output in the UI is formatted instead of raw JSON.
- 🩹 **Cleaner updates**: the old background CLI updater that wrote to `/usr/local/bin` was removed; `python3-websockets` is installed so the bundled skill works out of the box.

## 1.0.1 - Automatic FQDN Server Naming & Custom Server Name

- 🏷️ **Automatic Server Naming via HA FQDN**: Automatically discovers and configures the Home Assistant FQDN (from `external_url` or `internal_url`) as the T3 Code server display name and environment host ID.
- ⚙️ **Configurable Server Name**: Optional `server_name` setting in add-on configuration allows overriding the server name.
- 🔗 **Persistent Host Identifiers**: Directly manages `/etc/machine-info`, `/data/.t3/usage-host-id`, `/etc/hostname`, and persistent overlays.
- 🐙 **Enhanced GitHub CLI Symlink**: Guaranteed `/usr/bin/gh` binary path compatibility.


## 1.0.0 - Initial Lightweight T3 Code Release

- 🚀 **Dedicated T3 Code Server**: Powered by `t3 serve --lan --port 3773` with auto-project bootstrapping at `/homeassistant`.
- 📱 **1-Click Mobile App Pairing**: Instant pairing QR code and deep links for the official T3 Code mobile apps (iOS & Android).
- 🌐 **T3 Connect Remote Access**: Full integration with T3 Connect and relay tunnel service (`relay.t3.codes`) with status dashboard.
- 🐙 **GitHub CLI (`gh`) Integration**: Pre-installed standalone stripped GitHub CLI binary for direct git and repository management.
- 🪶 **Ultra Lightweight Footprint**: Stripped unnecessary heavy runtimes (saving >500 MB), base image built on minimal Debian Slim.
- 🔌 **Home Assistant Domotics MCP**: Native Model Context Protocol (MCP) server integration for Home Assistant Core and Zigbee2MQTT.
- 🖥️ **Ingress Web Terminal**: Fully-featured terminal UI with multi-session tabs, responsive D-Pad, theme toggles, and config backups.
