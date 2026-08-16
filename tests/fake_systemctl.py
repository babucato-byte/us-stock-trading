#!/usr/bin/env python3
"""A stand-in for `systemctl` and `systemd-analyze`, so the installer's
REAL control flow can be exercised on a machine without systemd.

Its behaviour is not invented: every response below was measured on the
Oracle host (Ubuntu 22.04.5, systemd 249) during the review of 7f532fc.

    unit WITHOUT [Install]:  is-enabled -> "static"   exit 0
                             enable     -> exit 0, warning, NO symlink
    unit WITH    [Install]:  is-enabled -> "disabled" exit 1
                             enable     -> creates a .wants/ symlink
                             is-enabled -> "enabled"  exit 0

State for the "host" (as opposed to a --root sandbox) lives in a JSON
file named by FAKE_SYSTEMCTL_STATE, so a test can seed it and inspect it
afterwards. FAKE_SYSTEMCTL_FAIL lists "verb:unit" pairs that must fail,
which is how the rollback paths get exercised.
"""
import json
import os
import re
import sys
from pathlib import Path

STATE_FILE = os.environ.get("FAKE_SYSTEMCTL_STATE", "")
UNIT_DIR = os.environ.get("FAKE_SYSTEMCTL_UNIT_DIR", "/etc/systemd/system")


def load():
    if STATE_FILE and Path(STATE_FILE).exists():
        return json.loads(Path(STATE_FILE).read_text())
    return {"enabled": {}, "active": {}, "calls": []}


def save(state):
    if STATE_FILE:
        Path(STATE_FILE).write_text(json.dumps(state, indent=2))


def has_install_section(path):
    if not path or not Path(path).exists():
        return None
    for line in Path(path).read_text().splitlines():
        if re.match(r"^\[Install\]", line.strip()):
            return True
    return False


def unit_path(root, name):
    base = Path(root) / "etc/systemd/system" if root else Path(UNIT_DIR)
    candidate = base / name
    return candidate if candidate.exists() else None


def main(argv):
    root = None
    args = []
    for arg in argv:
        if arg.startswith("--root="):
            root = arg[len("--root="):]
        elif arg.startswith("--"):
            continue
        else:
            args.append(arg)
    if not args:
        return 0
    verb, units = args[0], args[1:]

    state = load()
    if root is None:
        state["calls"].append(" ".join([verb, *units]))

    failures = set(filter(None, os.environ.get("FAKE_SYSTEMCTL_FAIL", "").split(",")))

    for unit in units:
        if f"{verb}:{unit}" in failures:
            print(f"fake systemctl: forced failure on {verb} {unit}", file=sys.stderr)
            save(state)
            return 1

    if verb == "daemon-reload":
        save(state)
        return 0

    if verb == "cat":
        for unit in units:
            if unit_path(root, unit) is None:
                print(f"No files found for {unit}.", file=sys.stderr)
                save(state)
                return 1
        save(state)
        return 0

    if verb == "is-enabled":
        rc = 0
        for unit in units:
            path = unit_path(root, unit)
            if path is None:
                print("not-found")
                rc = 1
                continue
            if root is None and state["enabled"].get(unit):
                print("enabled")
                continue
            if has_install_section(path):
                print("disabled")
                rc = 1
            else:
                print("static")
        save(state)
        return rc

    if verb == "is-active":
        rc = 0
        for unit in units:
            value = state["active"].get(unit, "inactive")
            print(value)
            if value != "active":
                rc = 1
        save(state)
        return rc

    if verb == "enable":
        for unit in units:
            path = unit_path(root, unit)
            if path is None:
                print(f"Failed to enable unit: Unit file {unit} does not exist.",
                      file=sys.stderr)
                save(state)
                return 1
            if not has_install_section(path):
                # Measured behaviour: warns, exits 0, creates nothing.
                print("The unit files have no installation config (WantedBy=, "
                      "RequiredBy=, Also=, Alias= settings in the [Install] section, "
                      "and DefaultInstance= for template units).", file=sys.stderr)
                continue
            if root is not None:
                wants = Path(root) / "etc/systemd/system/multi-user.target.wants"
                wants.mkdir(parents=True, exist_ok=True)
                link = wants / unit
                if not link.exists():
                    link.symlink_to(path)
            else:
                state["enabled"][unit] = True
        save(state)
        return 0

    if verb == "disable":
        for unit in units:
            state["enabled"].pop(unit, None)
        save(state)
        return 0

    if verb == "start":
        for unit in units:
            state["active"][unit] = "active"
        save(state)
        return 0

    if verb == "stop":
        for unit in units:
            state["active"][unit] = "inactive"
        save(state)
        return 0

    save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
