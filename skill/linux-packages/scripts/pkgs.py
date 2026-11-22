#!/usr/bin/env python3
"""Manage extra apt packages in the T3 Code Home Assistant add-on.

The single source of truth is the add-on option `extra_packages` managed by
the Home Assistant Supervisor. `list` and `ensure` read the live options from
`GET /addons/self/info` (falling back to /data/options.json when Supervisor is
unavailable), and `add` / `remove` update them through the Supervisor API
(`POST /addons/self/options`) so changes are validated, persisted, and visible
in the Configuration tab.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

OPTIONS_FILE = os.environ.get("OPTIONS_FILE", "/data/options.json")
SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor")
HA_TOKEN_FILE = os.environ.get("HA_TOKEN_FILE", "/data/.t3/ha.token")
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9+.-]*$")


def get_supervisor_token():
    """Return the Supervisor bearer token from env or token file, or ''."""
    for var in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        token = os.environ.get(var, "").strip()
        if token:
            return token
    try:
        with open(HA_TOKEN_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def supervisor_request(method, path, payload=None, timeout=15):
    """Send a JSON request to the Supervisor API and return the parsed body."""
    token = get_supervisor_token()
    if not token:
        raise RuntimeError(
            "Supervisor token is not available (SUPERVISOR_TOKEN/HASSIO_TOKEN/"
            f"{HA_TOKEN_FILE})."
        )

    url = SUPERVISOR_URL.rstrip("/") + path
    headers = {"Authorization": "Bearer " + token}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        detail = body or str(exc.reason)
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and parsed.get("message"):
                detail = str(parsed["message"])
        except ValueError:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc

    try:
        result = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid JSON from Supervisor: {exc}") from exc

    if not isinstance(result, dict) or result.get("result") not in ("ok", None):
        raise RuntimeError("Supervisor rejected request: " + str(result))
    return result


def read_options_from_api(timeout=5):
    """Fetch live add-on options from GET /addons/self/info."""
    resp = supervisor_request("GET", "/addons/self/info", timeout=timeout)
    data = resp.get("data")
    options = data.get("options") if isinstance(data, dict) else None
    if not isinstance(options, dict):
        raise RuntimeError("Supervisor response did not include add-on options.")
    return dict(options)


def read_options_from_file():
    """Read add-on options from OPTIONS_FILE as a fallback."""
    try:
        with open(OPTIONS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return dict(data) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_options():
    """Read live options from Supervisor API, falling back to OPTIONS_FILE."""
    if get_supervisor_token():
        try:
            return read_options_from_api(timeout=3)
        except RuntimeError:
            pass
    return read_options_from_file()


def extract_packages(options):
    """Return deduplicated package names from an options dictionary."""
    names = []
    for item in options.get("extra_packages") or []:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
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


def update_options_via_api(packages, adding):
    """Merge extra_packages against live Supervisor options and POST back."""
    if not get_supervisor_token():
        print(
            "Supervisor token is not available; cannot update the add-on options. "
            "Add or remove packages in Settings -> Add-ons -> T3 Code -> Configuration.",
            file=sys.stderr,
        )
        return False

    try:
        options = read_options_from_api(timeout=15)
    except RuntimeError as exc:
        print("Supervisor API read failed: " + str(exc), file=sys.stderr)
        return False

    current = extract_packages(options)
    if adding:
        updated = current + [p for p in packages if p not in current]
        if updated == current:
            print("Already in add-on option 'extra_packages': " + ", ".join(packages))
            return True
    else:
        remove_set = set(packages)
        updated = [p for p in current if p not in remove_set]
        if updated == current:
            print("Not in add-on option 'extra_packages': " + ", ".join(packages))
            return True

    new_options = dict(options)
    new_options["extra_packages"] = updated

    try:
        supervisor_request(
            "POST",
            "/addons/self/options",
            payload={"options": new_options},
            timeout=15,
        )
    except RuntimeError as exc:
        print("Supervisor API update failed: " + str(exc), file=sys.stderr)
        return False

    summary = ", ".join(updated) if updated else "(empty)"
    print("Updated add-on option 'extra_packages' via Supervisor API: " + summary)
    return True


def cmd_list(args):
    names = extract_packages(read_options())
    if not names:
        print("No extra packages configured (add-on option 'extra_packages' is empty).")
        return 0
    print("Configured packages (add-on option 'extra_packages'):")
    for name in names:
        print("  {:<30} {}".format(name, "installed" if is_installed(name) else "MISSING"))
    return 0


def cmd_ensure(args):
    targets = args.packages or extract_packages(read_options())
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
    if not args.no_install:
        missing = [p for p in args.packages if not is_installed(p)]
        if missing and not install(missing):
            return 1
    return 0 if update_options_via_api(args.packages, adding=True) else 1


def cmd_remove(args):
    if not check_names(args.packages):
        return 2
    return 0 if update_options_via_api(args.packages, adding=False) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show configured packages and their state").set_defaults(func=cmd_list)
    p = sub.add_parser("ensure", help="install missing packages now (does not change the options)")
    p.add_argument("packages", nargs="*")
    p.set_defaults(func=cmd_ensure)
    p = sub.add_parser("add", help="install and persist packages in add-on option extra_packages via Supervisor API")
    p.add_argument("--no-install", action="store_true", help="only update the add-on option without running apt-get")
    p.add_argument("packages", nargs="+")
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("remove", help="remove packages from add-on option extra_packages via Supervisor API")
    p.add_argument("packages", nargs="+")
    p.set_defaults(func=cmd_remove)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
