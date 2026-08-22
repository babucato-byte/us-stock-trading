"""Is S6-R ready to be promoted? Read-only; promotes nothing.

Why this exists as code
-----------------------
The activation gate has been assembled by hand across several reports,
and a human reading fourteen log sources is exactly where a NOT_MEASURED
quietly becomes a PASS. This evaluates every condition in one place and
distinguishes three answers rather than two.

NOT_MEASURED is not PASS
------------------------
The distinction is the whole point. "The market was closed so we could
not check" and "we checked and it was fine" are different facts, and only
one of them permits promotion. A weekend cannot manufacture either a
pass or a failure -- it produces NOT_MEASURED, and READY requires every
check to have actually run.

It does not promote
-------------------
`evaluate()` returns a verdict. Changing `scanner_live_mode` is a
separate, deliberate act performed after the verdict is read, because an
evaluator that flipped the flag itself would make the promotion a
consequence of running a report.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

PASS = "PASS"
FAIL = "FAIL"
NOT_MEASURED = "NOT_MEASURED"

READY = "READY"
NOT_READY = "NOT_READY"
BLOCKED = "BLOCKED"

#: Every condition §8 requires, in the order an operator would check
#: them: what is installed, then what is wired, then what only a live
#: market can confirm.
CHECKS = (
    "scheduler_installed",
    "scanner_cron_active",
    "runtime_cron_active",
    "runtime_loaded",
    "fill_sync_ready",
    "exit_runtime_ready",
    "common_sell_ready",
    "restart_recovery_ready",
    "reconciliation_healthy",
    "s1_healthy",
    "regression_healthy",
    "candidate_freshness_verified",
    "regular_market_tick_verified",
    "common_stock_dry_run_verified",
)

#: Checks that can only be answered by a live market. Named so a report
#: can say WHY something is unmeasured rather than leaving a reader to
#: infer it from the calendar.
MARKET_DEPENDENT = frozenset({
    "candidate_freshness_verified",
    "regular_market_tick_verified",
    "common_stock_dry_run_verified",
})


@dataclass
class Result:
    status: str
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status == PASS


@dataclass
class Readiness:
    checks: Dict[str, Result] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        """READY only when EVERY check passed.

        A FAIL is BLOCKED -- something is wrong and promoting would carry
        it into live trading. Anything merely unmeasured is NOT_READY:
        nothing is broken, the evidence simply does not exist yet, and
        those two need different responses from an operator.
        """
        if any(r.status == FAIL for r in self.checks.values()):
            return BLOCKED
        if all(r.status == PASS for r in self.checks.values()):
            return READY
        return NOT_READY

    @property
    def ready(self) -> bool:
        return self.verdict == READY

    def unmeasured(self) -> List[str]:
        return sorted(k for k, r in self.checks.items()
                      if r.status == NOT_MEASURED)

    def failures(self) -> List[str]:
        return sorted(k for k, r in self.checks.items() if r.status == FAIL)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "READY_FOR_S6_LIMITED_LIVE": self.ready,
            "checks": {k: {"status": r.status, "detail": r.detail}
                       for k, r in self.checks.items()},
            "unmeasured": self.unmeasured(),
            "failures": self.failures(),
        }


def _safe(name: str, probe: Callable[[], Result]) -> Result:
    """A probe that raises is NOT_MEASURED, never PASS.

    An evaluator whose own failure read as a pass would be worse than no
    evaluator -- it would answer confidently about a check it never ran.
    """
    try:
        return probe()
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness probe %s failed", name, exc_info=True)
        return Result(NOT_MEASURED, f"probe failed: {str(exc)[:120]}")


def evaluate(*, conn=None, crontab: Optional[str] = None,
             observations: Optional[Dict[str, Any]] = None) -> Readiness:
    """The full gate. `observations` supplies facts only a live run knows.

    Anything absent from `observations` is NOT_MEASURED rather than
    assumed -- which is what makes a weekend evaluation honest instead of
    optimistic.
    """
    seen = observations or {}
    checks: Dict[str, Result] = {}

    checks["scheduler_installed"] = _safe(
        "scheduler_installed", lambda: _cron_has(crontab, "s6_scan.sh")
        if crontab is not None else Result(NOT_MEASURED, "no crontab supplied"))
    checks["scanner_cron_active"] = _safe(
        "scanner_cron_active", lambda: _cron_has(crontab, "s6_scan.sh")
        if crontab is not None else Result(NOT_MEASURED, "no crontab supplied"))
    checks["runtime_cron_active"] = _safe(
        "runtime_cron_active", lambda: _cron_has(crontab, "s6_exec.sh")
        if crontab is not None else Result(NOT_MEASURED, "no crontab supplied"))

    checks["runtime_loaded"] = _safe("runtime_loaded", _runtime_loaded)
    checks["fill_sync_ready"] = _safe("fill_sync_ready", _fill_sync_ready)
    checks["exit_runtime_ready"] = _safe("exit_runtime_ready",
                                         _exit_runtime_ready)
    checks["common_sell_ready"] = _safe("common_sell_ready",
                                        _common_sell_ready)
    checks["restart_recovery_ready"] = _safe("restart_recovery_ready",
                                             _restart_ready)

    checks["reconciliation_healthy"] = _safe(
        "reconciliation_healthy",
        lambda: _reconciliation(conn, seen) if conn is not None
        else Result(NOT_MEASURED, "no database connection supplied"))
    checks["s1_healthy"] = _safe(
        "s1_healthy", lambda: _observed(seen, "s1_healthy"))
    checks["regression_healthy"] = _safe(
        "regression_healthy", lambda: _observed(seen, "regression_healthy"))

    for name in sorted(MARKET_DEPENDENT):
        checks[name] = _safe(name, lambda n=name: _observed(seen, n))

    return Readiness(checks={k: checks[k] for k in CHECKS})


def _observed(seen: Dict[str, Any], key: str) -> Result:
    """A fact only a live run can supply. Absent means unmeasured."""
    if key not in seen or seen[key] is None:
        return Result(NOT_MEASURED, "not observed in this evaluation")
    return (Result(PASS, "observed") if seen[key]
            else Result(FAIL, "observed and failing"))


def _cron_has(crontab: str, script: str) -> Result:
    for line in (crontab or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and script in stripped:
            return Result(PASS, stripped.split()[0])
    return Result(FAIL, f"{script} is not scheduled")


def _runtime_loaded() -> Result:
    from s6_live import candidate_source, exit_policy, position_store

    for module, attribute in ((candidate_source, "S6CandidateSource"),
                              (exit_policy, "decide"),
                              (position_store, "record_submission")):
        if not hasattr(module, attribute):
            return Result(FAIL, f"{module.__name__} lacks {attribute}")
    return Result(PASS, "source, policy and store import")


def _fill_sync_ready() -> Result:
    from s6_live import exit_runtime

    missing = [n for n in ("sync_buy_fills", "sync_sell_fills")
               if not callable(getattr(exit_runtime, n, None))]
    return (Result(FAIL, f"missing {missing}") if missing
            else Result(PASS, "buy and sell fill sync present"))


def _exit_runtime_ready() -> Result:
    from s6_live import exit_runtime

    missing = [n for n in ("run_exits", "retry_latched_exits")
               if not callable(getattr(exit_runtime, n, None))]
    return (Result(FAIL, f"missing {missing}") if missing
            else Result(PASS, "exit evaluation and latched retry present"))


def _common_sell_ready() -> Result:
    """The SELL must be the SHARED submitter, not an S6 copy."""
    import inspect

    from s6_live import exit_runtime

    source = inspect.getsource(exit_runtime)
    if "from s1_live.exit_runtime import" not in source:
        return Result(FAIL, "S6 does not use the shared submitter")
    if "submit_order" in source:
        return Result(FAIL, "S6 calls a broker directly")
    return Result(PASS, "uses s1_live.exit_runtime._submit_sell")


def _restart_ready() -> Result:
    from s6_live import position_store

    missing = [n for n in ("load_unconfirmed", "load_live",
                           "abandon_submission")
               if not callable(getattr(position_store, n, None))]
    return (Result(FAIL, f"missing {missing}") if missing
            else Result(PASS, "submitted, live and abandon paths present"))


def _reconciliation(conn, seen) -> Result:
    from reconciliation import internal_holdings

    account = seen.get("account_rows")
    if account is None:
        return Result(NOT_MEASURED, "no account rows supplied")
    summary = internal_holdings.summary(conn, account)
    if not summary["coverage_healthy"]:
        return Result(FAIL, f"coverage gaps: {summary['coverage_gaps']}")
    return Result(PASS, "; ".join(summary["attribution"]))
