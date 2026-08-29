#!/usr/bin/env python3
"""What each release on disk is, and whether anything still needs it.

Why an inventory before any deletion
------------------------------------
There are 76 releases at roughly 311MB each and the disk is 83% full.
The tempting fix is to delete the old ones. The reason not to do it
straight away is that a release directory is not just code: it holds the
venv the running crons resolve through `TRADING_PROJECT_ROOT`, and it is
the only artifact a rollback can point at. Deleting the wrong one breaks
production silently -- the next cron resolves a root that is not there
and refuses to run, which looks like a scanner problem.

So this REPORTS. It prints what is protected and why, and what is a
prune candidate, and it deletes nothing. Whoever runs the cleanup does
it afterwards, from a list they have read.

What is protected
-----------------
The deployed release, the validated one, the previous known-good for
rollback, and the most recent N. Everything else is a candidate --
"candidate" meaning eligible for a human to consider, not scheduled.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("release_retention")

RELEASES = Path("/home/ubuntu/releases/us-stock-trading")
ENV_FILE = RELEASES / "shared" / "env" / "kis-readonly.env"

#: Recent releases kept beyond the ones with a named role, so a rollback
#: has somewhere to go even if the "previous known-good" record is lost.
DEFAULT_KEEP_RECENT = 5

PROTECTED_DEPLOYED = "DEPLOYED"
PROTECTED_VALIDATED = "VALIDATED"
PROTECTED_ROLLBACK = "PREVIOUS_KNOWN_GOOD"
PROTECTED_RECENT = "RECENT"
PRUNE_CANDIDATE = "PRUNE_CANDIDATE"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _env_value(name, path=None):
    # Resolved at CALL time, not bound as a default: a default argument
    # freezes the module-level value at import, so pointing the tool at
    # a different environment file would silently keep reading the old
    # one -- and the report would name the wrong release as deployed.
    path = path if path is not None else ENV_FILE
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    except Exception:  # noqa: BLE001
        logger.warning("could not read %s from %s", name, path)
    return None


def _size_bytes(path):
    try:
        out = subprocess.run(["du", "-sb", str(path)], capture_output=True,
                             text=True, timeout=120)
        return int(out.stdout.split()[0]) if out.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def inventory(*, releases_dir=RELEASES, keep_recent=DEFAULT_KEEP_RECENT,
              with_sizes=True):
    """Every release, its role, and whether it may be pruned."""
    root = Path(releases_dir)
    found = [p for p in root.iterdir()
             if p.is_dir() and SHA_RE.match(p.name)] if root.exists() else []
    found.sort(key=_mtime, reverse=True)

    deployed = _env_value("DEPLOYED_COMMIT")
    validated = _env_value("VALIDATED_COMMIT")
    recent = {p.name for p in found[:keep_recent]}

    rows = []
    for path in found:
        roles = []
        if path.name == deployed:
            roles.append(PROTECTED_DEPLOYED)
        if path.name == validated:
            roles.append(PROTECTED_VALIDATED)
        if path.name in recent:
            roles.append(PROTECTED_RECENT)
        rows.append({
            "sha": path.name,
            "path": str(path),
            "modified": _mtime(path),
            "size_bytes": _size_bytes(path) if with_sizes else None,
            "roles": roles,
            "disposition": PRUNE_CANDIDATE if not roles else "PROTECTED",
        })

    # The newest release that is neither deployed nor validated is where
    # a rollback would go. Named explicitly so it is never a candidate
    # by accident.
    rollback = next((r for r in rows
                     if PROTECTED_DEPLOYED not in r["roles"]
                     and PROTECTED_VALIDATED not in r["roles"]), None)
    if rollback is not None and PROTECTED_ROLLBACK not in rollback["roles"]:
        rollback["roles"].append(PROTECTED_ROLLBACK)
        rollback["disposition"] = "PROTECTED"

    candidates = [r for r in rows if r["disposition"] == PRUNE_CANDIDATE]
    reclaimable = sum(r["size_bytes"] or 0 for r in candidates)
    return {
        "releases": len(rows),
        "deployed": deployed,
        "validated": validated,
        "keep_recent": keep_recent,
        "protected": len(rows) - len(candidates),
        "prune_candidates": len(candidates),
        "reclaimable_bytes": reclaimable,
        "rows": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report release retention. Deletes nothing.")
    parser.add_argument("--keep-recent", type=int, default=DEFAULT_KEEP_RECENT)
    parser.add_argument("--releases-dir", default=str(RELEASES))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-sizes", action="store_true",
                        help="skip du, which is the slow part")
    args = parser.parse_args(argv)
    logging.basicConfig(level="INFO", format="%(message)s")

    report = inventory(releases_dir=args.releases_dir,
                       keep_recent=args.keep_recent,
                       with_sizes=not args.no_sizes)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    logger.info("%d release(s); %d protected, %d prune candidate(s)",
                report["releases"], report["protected"],
                report["prune_candidates"])
    logger.info("deployed=%s validated=%s",
                (report["deployed"] or "?")[:12],
                (report["validated"] or "?")[:12])
    logger.info("reclaimable if all candidates removed: %.1f GB",
                report["reclaimable_bytes"] / 1e9)
    logger.info("")
    for row in report["rows"]:
        logger.info("  %-12s %-9s %6.0f MB  %s", row["sha"][:12],
                    row["disposition"].replace("PRUNE_CANDIDATE", "CANDIDATE"),
                    (row["size_bytes"] or 0) / 1e6,
                    ",".join(row["roles"]) or "-")
    logger.info("")
    logger.info("This command deletes nothing. Removal is a separate, "
                "reviewed step.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
