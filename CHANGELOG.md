# Changelog

## 1.1.17 (2026-11-22)

- 🔧 **Persist `extra_packages` via Supervisor API**: updated the `linux-packages` skill (`pkgs.py`) to read live options from `GET /addons/self/info` and write `extra_packages` via `POST /addons/self/options` instead of using `/data/.t3/extra-packages.list`, keeping the Home Assistant Configuration UI, backups, and skill in sync while preserving `!secret` references and existing options.
- ♻️ **Automatic one-time migration**: `install-extra-packages.sh` now fetches live options from the Supervisor API (falling back to `/data/options.json`), migrates any legacy `/data/.t3/extra-packages.list` entries into `extra_packages` via the Supervisor API on startup, and removes the legacy file once migrated.
- 🏷️ **Same-day nightly version bumps**: `.github/workflows/builder-nightly.yaml` now appends `.1`, `.2`, … on same-day nightly rebuilds (e.g., `0.1.20261122.1`) so Home Assistant always detects new nightly builds.

## 1.1.16 (2026-11-22)

- 📦 **Extra Linux packages option**: new `extra_packages` add-on setting (list of Debian package names, default empty). `install-extra-packages.sh` runs from `run.sh` on every start and installs only the packages missing in the freshly rebuilt container, so the list is reapplied after every add-on update. It also accepts packages appended by the skill, validates names and logs results to `/data/.t3/extra-packages.log`.
- 🛠️ **New `linux-packages` skill**: can `list`, `ensure`, `add` and `remove` packages, reinstall everything after an update, and — by instruction — always asks for confirmation before persisting a package to `/data/.t3/extra-packages.list`. Installed into all agent skill roots by the updated `skills.sh`.

## 1.1.15 (2026-11-21)

- 🅰️ **Second add-on: T3 Code (Nightly)** (`t3-ha-app-nightly`) with the newest `T3CODE_CHANNEL=nightly` build, rebuilt and published daily by `.github/workflows/builder-nightly.yaml` (amd64/aarch64 + multi-arch manifest, verified before handoff).
- 🗂️ **Shared data directory**: both add-ons keep their complete persistent state in `/share/t3-ha-app-data` (linked into `/data` by the new `shared-data.sh`); the nightly has no private data of its own, and the stable add-on migrates its existing `/data` once. `options.json` remains per-add-on.
- 🔀 **Shared defaults**: internal API/ttyd/nginx ports are configurable (`API_PORT`, `TTYD_PORT`, `NGINX_PORT`), with the nightly using the same defaults as the stable add-on (3774 / 62906 / 62905 / 62901) so it is a drop-in replacement.

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
