#!/usr/bin/env python3
"""Fail-closed retention for immutable production release directories.

Default is dry-run.  Only SHA-named children of the release root are ever
eligible; shared state, env, logs, and cron paths are outside this tool's
namespace by construction.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")
KEEP_RECENT, MAX_COUNT, TARGET_DISK = 5, 10, 75
BUNDLE_MIN_AGE_SECONDS = 24 * 60 * 60
MAX_LOG_BYTES = 2 * 1024 * 1024


def env_values(env_file):
    values = {}
    try:
        for line in Path(env_file).read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip()
    except OSError:
        return None
    return values


def disk_pct(path):
    usage = shutil.disk_usage(path)
    return int(usage.used * 100 / usage.total)


def process_release_paths(proc_root=Path("/proc")):
    """Return release paths referenced by live processes, or None on ambiguity."""
    paths = set()
    try:
        pids = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            for name in ("cwd", "exe"):
                target = os.readlink(pid / name)
                hit = re.search(r"(/home/ubuntu/releases/us-stock-trading/[0-9a-f]{40})(?:/|$)", target)
                if hit:
                    paths.add(hit.group(1))
            cmdline = (pid / "cmdline").read_text(errors="ignore")
            for hit in re.finditer(r"/home/ubuntu/releases/us-stock-trading/[0-9a-f]{40}", cmdline):
                paths.add(hit.group(0))
        except (OSError, UnicodeError):
            continue  # a process exiting during inspection is harmless
    return paths


def release_rows(root):
    try:
        rows = [p for p in Path(root).iterdir() if p.is_dir() and SHA.match(p.name)]
    except OSError:
        return None
    return sorted(rows, key=lambda p: p.stat().st_mtime)


def old_bundles(bundle_dir, *, active_paths=(), now=None):
    """Only aged deployment bundles; a process-referenced bundle is sacred."""
    current = time.time() if now is None else now
    active = {str(Path(p)) for p in active_paths}
    return [p for p in Path(bundle_dir).glob("*.bundle")
            if str(p) not in active and current - p.stat().st_mtime > BUNDLE_MIN_AGE_SECONDS]


def bounded_log(path, record):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
        backup = path.with_suffix(path.suffix + ".1")
        if backup.exists(): backup.unlink()
        path.replace(backup)
    with path.open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def alert_if_abnormal(report):
    """Best-effort only; normal maintenance stays silent."""
    abnormal = (report.get("status") == "RETENTION_SAFETY_ABORT" or
                bool(report.get("errors")) or
                report.get("disk_after_pct", report.get("disk_before_pct", 0)) >= 85 or
                (report.get("disk_before_pct", 0) >= 85 and not report.get("candidates")))
    if not abnormal:
        return False
    try:
        from slack_utils import send_kis_live_alert
        return bool(send_kis_live_alert("RELEASE_RETENTION_ALERT " + json.dumps(report, sort_keys=True)[:1500]))
    except Exception:
        return False


def plan(*, releases_root, env_file, proc_paths=None, now=None):
    root = Path(releases_root)
    env = env_values(env_file)
    rows = release_rows(root)
    if not env or rows is None:
        return {"status": "RETENTION_SAFETY_ABORT", "errors": ["release metadata unreadable"]}
    deployed, validated, current = (env.get("DEPLOYED_COMMIT"), env.get("VALIDATED_COMMIT"), env.get("TRADING_PROJECT_ROOT"))
    if not deployed or not validated or not current or not SHA.match(deployed) or not SHA.match(validated):
        return {"status": "RETENTION_SAFETY_ABORT", "errors": ["release metadata incomplete"]}
    by_sha = {p.name: p for p in rows}
    if deployed not in by_sha or validated not in by_sha or Path(current).name not in by_sha:
        return {"status": "RETENTION_SAFETY_ABORT", "errors": ["release SHA/path mapping missing"]}
    active = process_release_paths() if proc_paths is None else set(proc_paths)
    if active is None:
        return {"status": "RETENTION_SAFETY_ABORT", "errors": ["active-process release mapping unavailable"]}
    unknown = [p for p in active if Path(p).parent == root and Path(p).name not in by_sha]
    if unknown:
        return {"status": "RETENTION_SAFETY_ABORT", "errors": ["active release missing: " + ",".join(unknown)]}
    protected = {deployed, validated, Path(current).name}
    protected.update(p.name for p in rows[-KEEP_RECENT:])
    protected.update(Path(p).name for p in active if Path(p).parent == root)
    # rollback: newest non-current release, even if it is not among recent.
    for p in reversed(rows):
        if p.name not in {deployed, validated, Path(current).name}:
            protected.add(p.name); break
    before = disk_pct(root)
    trigger = []
    if len(rows) > MAX_COUNT: trigger.append("RELEASE_COUNT")
    if before >= 80: trigger.append("DISK_USAGE")
    candidates = [p for p in rows if p.name not in protected]
    return {"status": "DRY_RUN", "trigger_reason": trigger, "disk_before_pct": before,
            "release_count_before": len(rows), "protected_releases": sorted(protected),
            "candidates": [str(p) for p in candidates], "errors": []}


def execute(report):
    if report["status"] == "RETENTION_SAFETY_ABORT" or not report["trigger_reason"]:
        return report
    deleted, reclaimed = [], 0
    for raw in report["candidates"]:
        if report["release_count_before"] - len(deleted) <= MAX_COUNT and disk_pct(Path(raw).parent) <= TARGET_DISK:
            break
        path = Path(raw)
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        shutil.rmtree(path)
        deleted.append(str(path)); reclaimed += size
    root = Path(report["candidates"][0]).parent if report["candidates"] else None
    bundles = old_bundles(report.get("bundle_dir", "/tmp"), active_paths=report.get("active_bundles", ()))
    for bundle in bundles:
        reclaimed += bundle.stat().st_size; bundle.unlink()
    report.update(status="EXECUTED", deleted_releases=deleted, bytes_reclaimed=reclaimed,
                  release_count_after=(len(release_rows(root)) if root else report["release_count_before"]),
                  disk_after_pct=(disk_pct(root) if root else report["disk_before_pct"]), bundles_deleted=[str(p) for p in bundles])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(); mode.add_argument("--dry-run", action="store_true"); mode.add_argument("--execute", action="store_true")
    parser.add_argument("--releases-root", default="/home/ubuntu/releases/us-stock-trading")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--bundle-dir", default="/tmp")
    parser.add_argument("--log-file", default="/home/ubuntu/logs/release_retention.log")
    args = parser.parse_args(argv)
    root = Path(args.releases_root); env = args.env_file or root / "shared/env/kis-readonly.env"
    report = plan(releases_root=root, env_file=env)
    report["bundle_dir"] = args.bundle_dir
    report["bundles_deleted"] = []
    if args.execute: report = execute(report)
    bounded_log(args.log_file, report)
    alert_if_abnormal(report)
    print(json.dumps(report, sort_keys=True))
    return 2 if report["status"] == "RETENTION_SAFETY_ABORT" else 0

if __name__ == "__main__":
    raise SystemExit(main())
