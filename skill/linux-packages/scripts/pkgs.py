#!/usr/bin/env python3
"""Manage extra apt packages in the T3 Code Home Assistant add-on.

Read-only by default (`list`). Mutating commands are explicit subcommands:
`ensure` installs what is missing now, `add` installs and persists a package,
`remove` drops one from the skill-managed list.
"""
import argparse
import json
import os
import re
import subprocess
import sys

OPTIONS_FILE = os.environ.get("OPTIONS_FILE", "/data/options.json")
LIST_FILE = os.environ.get("EXTRA_PACKAGES_LIST", "/data/.t3/extra-packages.list")
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9+.-]*$")


def configured_packages():
    names = []
    try:
        with open(OPTIONS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        for item in data.get("extra_packages") or []:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
    except (OSError, ValueError):
        pass
    try:
        with open(LIST_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    names.append(line)
    except OSError:
        pass
    seen, unique = set(), []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def check_names(names):
    bad = [n for n in names if not NAME_RE.match(n)]
    if bad:
        print("Invalid package name(s): " + ", ".join(bad), file=sys.stderr)
        return False
    return True


def is_installed(pkg):
    try:
        out = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", pkg],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return "install ok installed" in out
    except Exception:
        return False


def install(pkgs):
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    print("Installing: " + " ".join(pkgs))
    if subprocess.run(["apt-get", "update", "-qq"], env=env).returncode != 0:
        print("apt-get update failed", file=sys.stderr)
        return False
    return subprocess.run(
        ["apt-get", "install", "-y", "--no-install-recommends", *pkgs], env=env
    ).returncode == 0


def append_to_list(pkgs):
    existing = configured_packages()
    to_append = [p for p in pkgs if p not in existing]
    if not to_append:
        print("Already in the configured list: " + ", ".join(pkgs))
        return
    os.makedirs(os.path.dirname(LIST_FILE), exist_ok=True)
    with open(LIST_FILE, "a", encoding="utf-8") as fh:
        fh.write("\n".join(to_append) + "\n")
    print("Persisted in " + LIST_FILE + ": " + ", ".join(to_append))


def cmd_list(args):
    names = configured_packages()
    if not names:
        print("No extra packages configured (add-on option 'extra_packages' and skill list are empty).")
        return 0
    print("Configured packages:")
    for name in names:
        print("  {:<30} {}".format(name, "installed" if is_installed(name) else "MISSING"))
    return 0


def cmd_ensure(args):
    targets = args.packages or configured_packages()
    if not targets:
        print("No packages to ensure.")
        return 0
    if not check_names(targets):
        return 2
    missing = [p for p in targets if not is_installed(p)]
    if not missing:
        print("All requested packages are already installed.")
        return 0
    return 0 if install(missing) else 1


def cmd_add(args):
    if not check_names(args.packages):
        return 2
    missing = [p for p in args.packages if not is_installed(p)]
    if missing and not install(missing):
        return 1
    append_to_list(args.packages)
    return 0


def cmd_remove(args):
    if not check_names(args.packages):
        return 2
    remove = set(args.packages)
    try:
        with open(LIST_FILE, encoding="utf-8") as fh:
            lines = [ln.rstrip("\n") for ln in fh]
    except OSError:
        lines = []
    kept = [
        ln for ln in lines
        if ln.strip() and ln.strip() not in remove and not ln.strip().startswith("#")
    ]
    os.makedirs(os.path.dirname(LIST_FILE), exist_ok=True)
    with open(LIST_FILE, "w", encoding="utf-8") as fh:
        if kept:
            fh.write("\n".join(kept) + "\n")
    print("Removed from the skill list: " + ", ".join(sorted(remove)))
    print("Packages from the add-on option 'extra_packages' are managed in the Home Assistant UI.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show configured packages and their state").set_defaults(func=cmd_list)
    p = sub.add_parser("ensure", help="install missing packages now (does not change the list)")
    p.add_argument("packages", nargs="*")
    p.set_defaults(func=cmd_ensure)
    p = sub.add_parser("add", help="install and persist packages in the skill list")
    p.add_argument("packages", nargs="+")
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("remove", help="remove packages from the skill list")
    p.add_argument("packages", nargs="+")
    p.set_defaults(func=cmd_remove)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
