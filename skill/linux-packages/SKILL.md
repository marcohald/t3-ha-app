---
name: linux-packages
description: >-
  Use when a task needs an additional Linux/apt package inside the T3 Code
  Home Assistant add-on, when an apt-installed tool vanished after an add-on
  update, or when the user asks to add/remove a package from the persistent
  package list. Installs missing packages immediately and only persists a
  package after explicitly asking the user.
metadata:
  version: "1"
---

# Linux packages in the T3 Code add-on

The add-on container is rebuilt on every add-on update. Anything installed with
`apt` disappears, so packages that must survive need to be listed in the
configuration. Two sources are combined:

1. The add-on option **`extra_packages`** — edited by the user in
   Settings → Add-ons → T3 Code → Configuration (Home Assistant UI).
2. The skill list at `/data/.t3/extra-packages.list` — managed by this skill
   after the user confirms.

`install-extra-packages.sh` runs from `run.sh` on every start and installs only
packages that are actually missing. A plain restart is a no-op; an add-on
update reinstalls the full list. The log is `/data/.t3/extra-packages.log`.

## Script

Use `scripts/pkgs.py` (python3, no extra dependencies):

| Task | Command |
| --- | --- |
| Show configured packages and which are missing | `python3 scripts/pkgs.py list` |
| Reinstall everything after an update | `python3 scripts/pkgs.py ensure` |
| Install one or more packages **now**, without persisting | `python3 scripts/pkgs.py ensure openssh-client` |
| Install **and** persist a package | `python3 scripts/pkgs.py add openssh-client` |
| Stop persisting a package from the skill list | `python3 scripts/pkgs.py remove openssh-client` |

Paths can be overridden with `OPTIONS_FILE`, `EXTRA_PACKAGES_LIST` for tests.

## Workflow — follow this order

1. **Check first.** Run `list` before installing anything. If the package is
   already present and configured, do nothing.
2. **Install what you need.** If a package is missing, run
   `ensure <package>...`. This never changes the persistent list.
3. **Always ask before persisting.** After a successful install, ask the user:
   *"Add **`<package>`** to the persistent package list so it is reinstalled
   after every add-on update?"*
   - **Yes** → run `add <package>...` and tell them the package is now in the
     skill list (and also visible via `list`).
   - **No** → leave it as a one-time install. Do not call `add`.
   - Never add a package without an explicit confirmation.
4. **Mention the UI alternative.** The user can also add packages themselves in
   Settings → Add-ons → T3 Code → Configuration → **Extra packages**. The skill
   list is merged with that option, not a replacement for it.
5. **After an add-on update**, if a previously working tool is gone, run
   `ensure` (no arguments) to reinstall everything from both sources.

## Rules

- Only valid Debian package names (`letters, digits, +, -, .`) are accepted;
  the script rejects anything else.
- The script never removes packages from the system — `remove` only stops
  future reinstallation.
- `apt-get` needs network access and root (the add-on runs as root). Failures
  are reported with a hint to the log; they never abort the add-on startup.
