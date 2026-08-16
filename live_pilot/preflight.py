"""T9: the pre-start checklist the real-time pilot must pass before it is
allowed to enter its loop. Any single FAIL and the pilot does not start.

This is NOT a second copy of `scripts/preflight_kis_live.py`. That one
asserts the read-only DEPLOYMENT posture on the Oracle host (every order
flag off, ENTRY_DISABLED on, units installed) -- by construction it can
only ever pass in a posture where nothing may trade. The pilot has to be
runnable in BOTH postures, so it checks a different thing: not "is
everything disabled?" but "is everything this session will actually rely
on present, fresh, readable and consistent RIGHT NOW?".

The eight gates, in the order they run:

    1. kis_env               paper or live; live needs an explicit ack
    2. live_response_pending the two KIS wire-format values that a real
                             response has not yet confirmed
    3. kill_switch           HALT clear, entries allowed
    4. reconciliation        snapshot fresh, clean, zero UNKNOWN, no HALT
    5. account               a real KIS account read, matching the
                             allow-listed account number
    6. scan_universe         the universe the scanner will scan
    7. watchlist             the candidates the entry path will evaluate
    8. runtime               flag consistency, log dir, no second pilot

Every gate reports a `reason_code` so a refusal is greppable, and no
gate prints a token, a full account number or a raw KIS response.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from broker.broker_config import env_bool
from domain.account_snapshot import CASH_AVAILABLE
from execution.secret_redaction import mask_account_number
from live_pilot import posture as posture_module

logger = logging.getLogger("live_pilot.preflight")

REPO_ROOT = Path(__file__).resolve().parent.parent

# Setting this to exactly "true" is the operator's acknowledgement that
# the pilot may read the REAL KIS account. It does not enable any order:
# ordering is still the three flags in live_pilot/posture.py.
ACK_LIVE_ENV = "LIVE_PILOT_ACK_LIVE_ENV"

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
RESULT_INFO = "INFO"
RESULT_WARN = "WARN"


@dataclass
class PreflightReport:
    rows: list = field(default_factory=list)

    def _add(self, status, name, reason_code, detail):
        self.rows.append({
            "check": name, "status": status,
            "reason_code": reason_code, "detail": detail,
        })

    def ok(self, name, detail=None, *, reason_code=None):
        self._add(RESULT_PASS, name, reason_code, detail)

    def fail(self, name, reason_code, detail=None):
        self._add(RESULT_FAIL, name, reason_code, detail)

    def info(self, name, reason_code, detail=None):
        """Recorded, visible, and NOT a refusal. Used only where a
        stricter reading would make the harness unable to run in the one
        posture that is sanctioned today (paper)."""
        self._add(RESULT_INFO, name, reason_code, detail)

    def warn(self, name, reason_code, detail=None):
        """Visible, recorded, and not a refusal for THIS posture -- but
        it does name something the next posture is still blocked on."""
        self._add(RESULT_WARN, name, reason_code, detail)

    @property
    def failures(self):
        return [row for row in self.rows if row["status"] == RESULT_FAIL]

    @property
    def warnings(self):
        return [row for row in self.rows if row["status"] == RESULT_WARN]

    @property
    def passed(self):
        return not self.failures

    def render(self):
        lines = []
        for row in self.rows:
            suffix = ""
            if row["reason_code"]:
                suffix += f" [{row['reason_code']}]"
            if row["detail"]:
                suffix += f" {row['detail']}"
            lines.append(f"[{row['status']}] {row['check']}{suffix}")
        return "\n".join(lines)

    def as_dict(self):
        return {
            "passed": self.passed,
            "failures": [row["check"] for row in self.failures],
            "rows": list(self.rows),
        }


# ---------------------------------------------------------------------
# 1. Which KIS environment this pilot will talk to.
# ---------------------------------------------------------------------
def check_kis_env(report, env):
    raw = (env.get("KIS_ENV") or "").strip().lower()
    if raw not in ("paper", "live"):
        report.fail("kis_env", "KIS_ENV_INVALID",
                    f"KIS_ENV must be 'paper' or 'live', got {raw or '<unset>'}")
        return None
    if raw == "live" and not env_bool(env, ACK_LIVE_ENV, False):
        report.fail(
            "kis_env", "LIVE_ENV_NOT_ACKNOWLEDGED",
            f"KIS_ENV=live requires {ACK_LIVE_ENV}=true (the pilot will read the "
            "REAL account); start with KIS_ENV=paper to test against 모의투자 first",
        )
        return raw
    report.ok("kis_env", f"KIS_ENV={raw}")
    return raw


# ---------------------------------------------------------------------
# 2. The wire-format values a real KIS response has not yet confirmed.
#    BACKLOG T9 calls these "TBD_VERIFY_LIVE_DOCS 2건". The marker itself
#    is gone (tests/test_kis_verification_matrix.py fixes its absence);
#    brokers.kis_broker.VERIFICATION_MATRIX is now the authority, and
#    LIVE_RESPONSE_PENDING_ITEMS is the derived list of what is still
#    unconfirmed. Reading the matrix here is documentation-as-data, not a
#    runtime switch inside kis_broker -- the constraint that test
#    enforces is about that module, not about a consumer of it.
# ---------------------------------------------------------------------
def check_live_response_pending(report, kis_env, posture=None):
    """Gates on what THIS posture actually uses.

    It used to gate on the whole matrix, on the reasoning that a
    hand-written relevance map could un-gate a value that mattered. The
    map is no longer hand-written: `required_for` lives on the matrix
    entry itself and is derived from where the value is REFERENCED, with
    tests/test_kis_verification_matrix.py proving the classification
    against the source. So the reason for the blanket rule is gone,
    while its cost was real -- OBSERVE reads nothing through the order
    or cancel endpoints, and refusing to observe because an order-only
    TR_ID is unconfirmed blocked the one activity that could confirm
    anything.

    OBSERVE requirements are still enforced exactly as before. ARMED-only
    pending values are reported, loudly, as what ARMED remains blocked
    on.
    """
    from brokers.kis_broker import (
        REQUIRED_FOR_ARMED, REQUIRED_FOR_OBSERVE, matrix_entries_for,
        pending_items_for,
    )

    resolved = posture or posture_module.resolve_posture().posture
    needed = REQUIRED_FOR_ARMED if resolved == posture_module.POSTURE_ARMED \
        else REQUIRED_FOR_OBSERVE
    pending = list(pending_items_for(needed))
    confirmed = len(matrix_entries_for(needed)) - len(pending)

    if pending:
        listed = ", ".join(pending)
        if kis_env == "live":
            report.fail(
                "live_response_pending", "LIVE_RESPONSE_PENDING",
                f"{len(pending)} of {resolved}'s KIS value(s) unconfirmed by a real "
                f"response ({listed}) -- confirm them with a read-only probe "
                "(scripts/verify_kis_observe_responses.py) or on 모의투자 and mark them "
                "LIVE_RESPONSE_CONFIRMED in brokers/kis_broker.py; there is no "
                "environment variable that skips this",
            )
            return
        # Paper is the only sanctioned way to confirm some of these at
        # all: the runbook forbids confirming the cancel TR_ID with a
        # real order. A paper pilot IS the confirming procedure, so
        # refusing to run it because the confirmation has not happened
        # would be circular.
        report.info(
            "live_response_pending", "LIVE_RESPONSE_PENDING_PAPER_OK",
            f"{len(pending)} value(s) still unconfirmed ({listed}) -- allowed on paper, "
            "and confirming them here is the point of a paper pilot",
        )
    else:
        report.ok(
            "live_response_pending",
            f"{resolved} requires {confirmed} wire value(s); all live-confirmed",
        )

    # What the NEXT posture is still blocked on. Never a refusal here --
    # this run is not armed -- but it must be visible, or an operator
    # could read a clean OBSERVE preflight as readiness to arm.
    if resolved != posture_module.POSTURE_ARMED:
        armed_pending = list(pending_items_for(REQUIRED_FOR_ARMED))
        armed_total = len(matrix_entries_for(REQUIRED_FOR_ARMED))
        if armed_pending:
            report.warn(
                "armed_response_requirements", "BLOCKED_FOR_ARMED_ONLY",
                f"{len(armed_pending)} of {armed_total} ARMED wire value(s) remain "
                f"unconfirmed ({', '.join(armed_pending)}); OBSERVE is allowed, "
                "ARMED stays blocked until every one is live-confirmed",
            )
        else:
            report.ok("armed_response_requirements",
                      f"all {armed_total} ARMED wire value(s) are live-confirmed")


# ---------------------------------------------------------------------
# 3. HALT / ENTRY_OFF.
# ---------------------------------------------------------------------
def check_kill_switch(report):
    """Fail-closed on anything that is not an explicit boolean False.

    The `type(...) is bool` check is the same discipline
    scripts/run_shadow_exit_evaluation.py::read_halt_state() applies:
    bool subclasses int, and every "I do not know" shape (None, 0, [],
    {}) is falsy -- so a coerced read would report the single most
    dangerous answer available here, "not halted".
    """
    try:
        from operations import kill_switch

        halted = kill_switch.is_halted()
        entry_allowed = kill_switch.is_entry_allowed()
    except Exception as exc:  # noqa: BLE001 -- unreadable is not "clear"
        report.fail("kill_switch", "HALT_STATUS_UNAVAILABLE", type(exc).__name__)
        return
    if type(halted) is not bool or type(entry_allowed) is not bool:
        report.fail(
            "kill_switch", "HALT_STATUS_INVALID",
            f"is_halted()={type(halted).__name__} "
            f"is_entry_allowed()={type(entry_allowed).__name__}, expected bool",
        )
        return
    if halted:
        report.fail("kill_switch", "HALT_ACTIVE", "operations HALT is set")
        return
    if not entry_allowed:
        report.fail("kill_switch", "ENTRY_OFF", "kill_switch_state blocks new entries")
        return
    report.ok("kill_switch", "halt=false entry_allowed=true")


# ---------------------------------------------------------------------
# 4. The reconciliation snapshot every gate downstream relies on.
# ---------------------------------------------------------------------
def check_reconciliation(report):
    from reconciliation import freshness

    try:
        result = freshness.evaluate(require_unknown_zero=True, require_halt_clear=True)
    except freshness.SnapshotUnusable as exc:
        report.fail("reconciliation", exc.reason_code, exc.detail)
        return
    report.ok("reconciliation",
              " ".join(f"{k}={v}" for k, v in result.as_log_fields().items()))


# ---------------------------------------------------------------------
# 5. A real account read. Not "credentials look present" -- an actual
#    call, because an unreachable or misconfigured account is exactly
#    what a start-up check exists to catch before an hours-long loop.
# ---------------------------------------------------------------------
def check_account(report, env, broker):
    if broker is None:
        report.fail("account", "BROKER_UNAVAILABLE",
                    "a KIS client could not be constructed")
        return None
    try:
        snapshot = broker.get_account_snapshot()
    except Exception as exc:  # noqa: BLE001 -- any read failure blocks the start
        report.fail("account", getattr(exc, "reason_code", "ACCOUNT_READ_FAILED"),
                    type(exc).__name__)
        return None
    allowed = (env.get("KIS_ALLOWED_ACCOUNT_NO") or "").strip()
    if not allowed:
        report.fail("account", "ACCOUNT_UNCONFIGURED",
                    "KIS_ALLOWED_ACCOUNT_NO is not set")
        return None
    if str(snapshot.account_id) != allowed:
        report.fail(
            "account", "ACCOUNT_MISMATCH",
            f"the account read back ({mask_account_number(snapshot.account_id)}) is not "
            f"KIS_ALLOWED_ACCOUNT_NO ({mask_account_number(allowed)})",
        )
        return None
    # ORACLE-CASH-01: the balance read does not carry a cash figure, so
    # this reports the STATUS rather than printing a bare `None` that
    # reads like "$0". Orderable cash is established per candidate at
    # entry time (TTTS3007R), not here -- and this is deliberately not a
    # failed gate: OBSERVE must still start so an operator can watch the
    # per-candidate reads happen.
    if snapshot.cash_status == CASH_AVAILABLE:
        cash_detail = f"orderable_usd={snapshot.usd_orderable_cash}"
    else:
        cash_detail = (
            f"orderable_usd=UNAVAILABLE ({snapshot.cash_source}); "
            "sized per candidate at entry time"
        )
    report.ok("account", f"account={mask_account_number(snapshot.account_id)} {cash_detail}")
    return snapshot


# ---------------------------------------------------------------------
# 6/7. What the scanner will scan, and what the entry path will evaluate.
# ---------------------------------------------------------------------
def check_scan_universe(report):
    try:
        from daily_candidate_scanner import load_scan_universe

        frame, source = load_scan_universe()
    except Exception as exc:  # noqa: BLE001
        report.fail("scan_universe", "UNIVERSE_UNREADABLE", type(exc).__name__)
        return 0
    rows = len(frame)
    if rows == 0:
        # T8's filter honours a zero-row result as a real answer ("the
        # account can afford nothing right now"). That answer is correct
        # and it also means a pilot session would scan nothing, so it is
        # a refusal to START, not a silent no-op session.
        report.fail("scan_universe", "UNIVERSE_EMPTY",
                    f"{Path(source).name} has zero rows")
        return 0
    report.ok("scan_universe", f"{rows} symbols from {Path(source).name}")
    return rows


def check_watchlist(report, *, scan_enabled):
    try:
        import paper_strategy_order as pso

        symbols = pso.load_watchlist()
    except Exception as exc:  # noqa: BLE001
        report.fail("watchlist", "WATCHLIST_UNREADABLE", type(exc).__name__)
        return []
    if not symbols and not scan_enabled:
        report.fail(
            "watchlist", "WATCHLIST_EMPTY",
            "no candidates and scanning is disabled -- this session would evaluate "
            "nothing; run the scanner first or start the pilot with --scan-interval",
        )
        return []
    if not symbols:
        report.info("watchlist", "WATCHLIST_EMPTY_SCAN_PENDING",
                    "empty; the first tick's scan will populate it")
        return []
    report.ok("watchlist", f"{len(symbols)} candidate(s)")
    return symbols


# ---------------------------------------------------------------------
# 8. Runtime preconditions.
# ---------------------------------------------------------------------
def check_flag_consistency(report, env):
    contradiction = posture_module.contradictory_posture(env)
    if contradiction:
        report.fail("flag_consistency", "CONTRADICTORY_POSTURE", contradiction)
        return
    decision = posture_module.resolve_posture(env)
    report.ok("flag_consistency",
              f"posture={decision.posture} ({decision.reason})")


def check_log_dir(report, log_dir):
    path = Path(log_dir)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".live_pilot_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        report.fail("log_dir", "LOG_DIR_UNWRITABLE", f"{path}: {exc}")
        return
    report.ok("log_dir", str(path))


def check_no_other_run(report):
    """The shared single-run lock is taken and released immediately.

    The pilot must NOT hold it for the session: reconciliation, the
    Shadow timer and the health report all take the same lock, and a
    multi-hour holder would starve every one of them. The pilot's own
    exclusivity is a separate lock file held by live_pilot.runner.
    """
    from execution import idempotency

    try:
        with idempotency.single_run_lock(timeout=1.0):
            pass
    except idempotency.IdempotencyError as exc:
        report.fail("single_run_lock", "ANOTHER_INSTANCE", str(exc))
        return
    report.ok("single_run_lock", "no other trading process holds the shared lock")


def run_preflight(*, env=None, broker=None, log_dir=None, scan_enabled=True):
    """Runs every gate and returns the report. Never raises for a failed
    CHECK -- a failure is a row, so the operator sees all of them at once
    instead of fixing them one restart at a time."""
    mapping = os.environ if env is None else env
    report = PreflightReport()

    kis_env = check_kis_env(report, mapping)
    check_live_response_pending(report, kis_env,
                                posture=posture_module.resolve_posture(mapping).posture)
    check_kill_switch(report)
    check_reconciliation(report)
    check_account(report, mapping, broker)
    check_scan_universe(report)
    check_watchlist(report, scan_enabled=scan_enabled)
    check_flag_consistency(report, mapping)
    check_log_dir(report, log_dir or default_log_dir(mapping))
    check_no_other_run(report)
    return report


def default_log_dir(env=None):
    mapping = os.environ if env is None else env
    raw = (mapping.get("LIVE_PILOT_LOG_DIR") or "").strip()
    if raw:
        return Path(raw)
    return REPO_ROOT / "logs" / "live_pilot"
