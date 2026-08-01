#!/usr/bin/env python3
"""CODEX-049: the pre-start gate every Oracle service runs before it is
allowed to start (systemd `ExecStartPre=`). Exits non-zero -- and
therefore prevents the unit from starting -- if ANY of the following is
not true:

    every required environment variable is present
    the KIS account alias is configured (so logs never need the number)
    Alpaca order submission is disabled
    KIS live order submission is disabled
    ENTRY_DISABLED is true
    LIVE_ROLLOUT_ENABLED is false
    the four LIVE_ENABLE_* exit flags are false
    the state DB is at the current migration version
    the reconciliation entrypoint is importable and its state file writable
    the log directory is writable
    no other instance already holds the single-run lock
    VALIDATED_COMMIT == DEPLOYED_COMMIT == the actual checked-out commit

It prints only variable NAMES and pass/fail -- never a secret's value,
and never a full account number (`--verbose` shows the masked last-4
form only). Run it by hand at any time; it makes no network calls and
mutates nothing except applying pending DB migrations, which is itself
one of the checks.

    python3 scripts/preflight_kis_live.py            # readonly-posture check
    python3 scripts/preflight_kis_live.py --verbose  # + masked detail
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution.secret_redaction import (  # noqa: E402
    account_alias,
    install_logging_redaction,
    mask_account_number,
)

REQUIRED_ENV_VARS = (
    "EXECUTION_BROKER",
    "KIS_ENV",
    "KIS_APP_KEY",
    "KIS_APP_SECRET",
    "KIS_ACCOUNT_NO",
    "KIS_ACCOUNT_PRODUCT_CD",
    "KIS_ALLOWED_ACCOUNT_NO",
    "VALIDATED_COMMIT",
    "DEPLOYED_COMMIT",
)

# Every flag that MUST be false/disabled for the read-only posture the
# initial Oracle deployment runs in.
REQUIRED_FALSE_FLAGS = (
    "ALPACA_ORDER_ENABLED",
    "ALPACA_PAPER_ORDER_ENABLED",
    "KIS_LIVE_ORDER_ENABLED",
    "LIVE_ROLLOUT_ENABLED",
    "LIVE_ENABLE_PARTIAL_PROFIT",
    "LIVE_ENABLE_TRAILING_STOP",
    "LIVE_ENABLE_TIME_STOP",
    "LIVE_ENABLE_EOD_EXIT",
)

REQUIRED_TRUE_FLAGS = ("ENTRY_DISABLED",)


class PreflightFailure(Exception):
    pass


def _is_true(raw):
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _is_false(raw):
    """Absent counts as false -- an unset order flag is disabled, and
    fail-closed defaults are the whole point. An unparseable value does
    NOT count as false."""
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() in ("0", "false", "no", "off")


def check_required_env(results, env):
    missing = [name for name in REQUIRED_ENV_VARS if not env.get(name)]
    if missing:
        results.fail("required_env", f"missing required environment variables: {sorted(missing)}")
    else:
        results.ok("required_env", f"{len(REQUIRED_ENV_VARS)} required variables present")


def check_account_alias(results, env, verbose):
    alias = account_alias()
    if not alias:
        results.fail("account_alias", "KIS_ACCOUNT_ALIAS is not set")
        return
    detail = f"alias={alias!r}"
    if verbose:
        detail += f" account={mask_account_number(env.get('KIS_ACCOUNT_NO'))}"
    results.ok("account_alias", detail)


def check_safety_flags(results, env):
    wrong = [name for name in REQUIRED_FALSE_FLAGS if not _is_false(env.get(name))]
    if wrong:
        results.fail("order_flags_disabled", f"these must be false but are not: {sorted(wrong)}")
    else:
        results.ok("order_flags_disabled", "all order/rollout/exit flags are disabled")

    not_true = [name for name in REQUIRED_TRUE_FLAGS if not _is_true(env.get(name))]
    if not_true:
        results.fail("entry_disabled", f"these must be true but are not: {sorted(not_true)}")
    else:
        results.ok("entry_disabled", "ENTRY_DISABLED=true")


def check_flag_consistency(results, env):
    """The specific inconsistency the directive calls out: a live order
    flag turned on while ENTRY_DISABLED is also on is a contradictory
    posture -- somebody half-enabled live trading. Refuse to start."""
    live_on = _is_true(env.get("KIS_LIVE_ORDER_ENABLED"))
    entry_disabled = _is_true(env.get("ENTRY_DISABLED"))
    if live_on and entry_disabled:
        results.fail(
            "flag_consistency",
            "KIS_LIVE_ORDER_ENABLED=true while ENTRY_DISABLED=true -- contradictory posture",
        )
        return
    if live_on and not _is_true(env.get("LIVE_ROLLOUT_ENABLED")):
        results.fail(
            "flag_consistency",
            "KIS_LIVE_ORDER_ENABLED=true while LIVE_ROLLOUT_ENABLED is not true",
        )
        return
    results.ok("flag_consistency", "order/entry/rollout flags are mutually consistent")


def check_commit_match(results, env, repo_root=REPO_ROOT):
    validated = env.get("VALIDATED_COMMIT", "")
    deployed = env.get("DEPLOYED_COMMIT", "")
    if not validated or validated != deployed:
        results.fail(
            "commit_match",
            f"VALIDATED_COMMIT ({validated[:12]!r}) != DEPLOYED_COMMIT ({deployed[:12]!r})",
        )
        return
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        results.fail("commit_match", f"could not read the checked-out commit: {exc}")
        return
    if not head.startswith(deployed) and not deployed.startswith(head):
        results.fail(
            "commit_match",
            f"DEPLOYED_COMMIT {deployed[:12]!r} is not the checked-out commit {head[:12]!r}",
        )
        return
    results.ok("commit_match", f"validated == deployed == HEAD ({head[:12]})")


def check_db_migrations(results):
    from state_store import db as state_db
    from state_store.migrations import CURRENT_SCHEMA_VERSION

    try:
        conn = state_db.open_db()
    except Exception as exc:
        results.fail("db_migrations", f"could not open the state database: {exc}")
        return
    try:
        version = state_db.get_schema_version(conn)
    finally:
        conn.close()
    if version != CURRENT_SCHEMA_VERSION:
        results.fail(
            "db_migrations",
            f"schema version {version} != expected {CURRENT_SCHEMA_VERSION}",
        )
        return
    results.ok("db_migrations", f"schema at version {version}")


def check_reconciliation_runnable(results):
    """Imports the reconciliation entrypoints and proves the durable
    reconciliation-state file's directory is writable. Deliberately makes
    NO network call -- preflight must not depend on KIS being reachable."""
    try:
        from reconciliation import reconciliation_state, snapshot  # noqa: F401
    except Exception as exc:
        results.fail("reconciliation_runnable", f"reconciliation modules do not import: {exc}")
        return
    state_path = reconciliation_state._resolve_state_path()
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        probe = state_path.parent / ".preflight_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        results.fail("reconciliation_runnable", f"reconciliation state dir not writable: {exc}")
        return
    results.ok("reconciliation_runnable", f"state dir writable ({state_path.parent})")


def check_log_dir_writable(results, env):
    log_dir = Path(env.get("TRADING_LOG_DIR") or (REPO_ROOT / "logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        probe = log_dir / ".preflight_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        results.fail("log_dir_writable", f"{log_dir} is not writable: {exc}")
        return
    results.ok("log_dir_writable", str(log_dir))


def check_single_run_lock(results):
    from execution import idempotency

    try:
        with idempotency.single_run_lock(timeout=1.0):
            pass
    except idempotency.IdempotencyError as exc:
        results.fail("single_run_lock", f"another instance appears to be running: {exc}")
        return
    results.ok("single_run_lock", "no other instance holds the single-run lock")


class Results:
    def __init__(self):
        self.rows = []

    def ok(self, name, detail):
        self.rows.append((name, True, detail))

    def fail(self, name, detail):
        self.rows.append((name, False, detail))

    @property
    def failures(self):
        return [row for row in self.rows if not row[1]]

    def render(self):
        lines = []
        for name, passed, detail in self.rows:
            lines.append(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}")
        return "\n".join(lines)


def run_preflight(env=None, *, verbose=False):
    """Returns a Results object. Never raises for a failed CHECK -- a
    failure is a recorded row, so the caller sees every problem at once
    rather than only the first."""
    env = os.environ if env is None else env
    results = Results()
    check_required_env(results, env)
    check_account_alias(results, env, verbose)
    check_safety_flags(results, env)
    check_flag_consistency(results, env)
    check_commit_match(results, env)
    check_db_migrations(results)
    check_reconciliation_runnable(results)
    check_log_dir_writable(results, env)
    check_single_run_lock(results)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="KIS live-deployment preflight checks")
    parser.add_argument("--verbose", action="store_true", help="show masked account detail")
    args = parser.parse_args(argv)

    install_logging_redaction()
    results = run_preflight(verbose=args.verbose)
    print(results.render())
    if results.failures:
        print(f"\nPREFLIGHT FAILED: {len(results.failures)} check(s) did not pass -- refusing to start.")
        return 1
    print("\nPREFLIGHT OK: safe read-only posture confirmed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
