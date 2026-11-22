---
name: linux-packages
description: >-
  Use when a task needs an additional Linux/apt package inside the T3 Code
  Home Assistant add-on, when an apt-installed tool vanished after an add-on
  update, or when the user asks to add/remove a package from the persistent
  package list. Installs missing packages immediately and persists them to the
  add-on's `extra_packages` option via the Supervisor API (asking first unless
  the user explicitly asked to persist).
metadata:
  version: "2"
---

# Linux packages in the T3 Code add-on

The add-on container is rebuilt on every add-on update. Anything installed with
`apt` disappears, so packages that must survive are stored in the add-on option
**`extra_packages`** (Settings → Add-ons → T3 Code → Configuration). That option
is the single source of truth. When this skill persists or removes a package, it
reads live options via `GET /addons/self/info` and writes the updated option
through the **Supervisor API** (`POST /addons/self/options`), the same endpoint
the Home Assistant UI uses — never to `/data/options.json` directly and never to
a side-car file. The change is validated by the Supervisor, immediately visible
in the Configuration tab, and included in backups.

`install-extra-packages.sh` runs from `run.sh` on every start and installs only
packages that are actually missing. A plain restart is a no-op; an add-on
update reinstalls the full list. The log is `/data/.t3/extra-packages.log`.

## Script

Use `scripts/pkgs.py` (python3, standard library only):

| Task | Command |
| --- | --- |
| Show configured packages and which are missing | `python3 scripts/pkgs.py list` |
| Reinstall everything after an update | `python3 scripts/pkgs.py ensure` |
| Install one or more packages **now**, without persisting | `python3 scripts/pkgs.py ensure openssh-client` |
| Install **and** persist a package in `extra_packages` via the Supervisor API | `python3 scripts/pkgs.py add openssh-client` |
| Remove a package from `extra_packages` via the Supervisor API | `python3 scripts/pkgs.py remove openssh-client` |

`pkgs.py` authenticates with `SUPERVISOR_TOKEN` (falling back to `HASSIO_TOKEN`
or `HA_TOKEN_FILE` / `/data/.t3/ha.token`). `list` and `ensure` read live options
from `GET /addons/self/info` and fall back to `/data/options.json` when
Supervisor is unreachable. Paths and endpoints can be overridden with
`SUPERVISOR_URL`, `OPTIONS_FILE`, and `HA_TOKEN_FILE` for tests.

## Workflow — follow this order

1. **Check first.** Run `list` before installing anything. If the package is
   already present and configured, do nothing.
2. **Install what you need.** If a package is missing and the user has not yet
   asked to make it permanent, run `ensure <package>...` first. This never
   changes the add-on options.
3. **Confirm before persisting (unless explicitly asked).** If the user already
   explicitly asked to make the package permanent/persistent, run `add <package>...`.
   Otherwise, after a successful install, ask the user:
   *"Add **`<package>`** to the add-on's **`extra_packages`** option so it is
   reinstalled after every add-on update?"*
   - **Yes** → run `add <package>...` and confirm it was persisted to
     `extra_packages` via the Supervisor API (and is now visible in `list` and
     the add-on Configuration tab).
   - **No** → leave it as a one-time install. Do not call `add`.
4. **Mention the UI alternative.** The user can also edit **Extra packages**
   (`extra_packages`) directly in Settings → Add-ons → T3 Code → Configuration;
   both the UI and `pkgs.py` read and update that same add-on option.
5. **After an add-on update**, if a previously working tool is gone, run
   `ensure` (no arguments) to reinstall everything configured in `extra_packages`.

## Rules

- Only valid Debian package names (`letters, digits, +, -, .`) are accepted;
  the script rejects anything else.
- Never edit `/data/options.json` directly or create side-car package list files.
- The script never removes packages from the system — `remove` only stops
  future reinstallation by updating `extra_packages` through the Supervisor API.
- `apt-get` needs network access and root (the add-on runs as root). Failures
  are reported with a hint to the log; they never abort the add-on startup.
