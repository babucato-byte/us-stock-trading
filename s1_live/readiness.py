"""Is S1 ready to go live, and to what stage? One place that answers.

Two things this module is careful about
---------------------------------------
1. Every requirement named in `config/s1_rollout_stages.py` is evaluated
   here. A requirement that were listed there and never checked would be
   a gate that reads like protection and enforces nothing, so
   `build_matrix()` covers the whole set and a test asserts the two lists
   agree.

2. Anything not proven READY blocks. There are four non-ready states and
   they are kept apart because the operator action differs:

       UNVERIFIED  the fact needs a real trade to establish
       UNKNOWN     it could not be measured right now
       BLOCKED     a decision has not been made (the S1 exit policy)
       DISABLED    deliberately off (the live rollout flag)

   None of them promote. Only READY does.

Verification that only a real order can produce
-----------------------------------------------
`position_valuation` and `reserved_order_cash` are claims about what the
BROKER does, and no amount of testing establishes them. They live in
`s1_verification_state`, default to UNVERIFIED, and a MISMATCH LATCHES --
a later clean read does not clear a valuation disagreement, because the
disagreement means the two sides were measuring different things and one
clean sample does not disprove that.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import s1_rollout_stages as stages

logger = logging.getLogger(__name__)

READY = "READY"
UNVERIFIED = "UNVERIFIED"
UNKNOWN = "UNKNOWN"
BLOCKED = "BLOCKED"
DISABLED = "DISABLED"

#: A valuation disagreement. Distinct from UNVERIFIED: one means "not
#: looked at yet", the other means "looked at, and the two sides
#: disagreed", and only the second is evidence of a defect.
MISMATCH = "MISMATCH"

#: Only this one permits promotion.
PROMOTING_STATUSES = frozenset({READY})

VERIFICATION_KEYS = (stages.REQ_POSITION_VALUATION, stages.REQ_RESERVED_ORDER_CASH)

STATE_TABLE = "s1_verification_state"


@dataclass
class Check:
    key: str
    status: str
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.status in PROMOTING_STATUSES

    def as_dict(self):
        return {"key": self.key, "status": self.status, "detail": self.detail}


@dataclass
class Matrix:
    checks: List[Check] = field(default_factory=list)
    generated_at: str = ""

    def by_key(self) -> Dict[str, Check]:
        return {check.key: check for check in self.checks}

    def unmet_for(self, stage) -> List[Check]:
        """Requirements of `stage` that are not READY."""
        lookup = self.by_key()
        unmet = []
        for key in stages.requirements_for(stage):
            check = lookup.get(key)
            if check is None:
                # A requirement with no evaluator is treated as unmet, not
                # as satisfied. The alternative silently passes any gate
                # someone forgot to implement.
                unmet.append(Check(key, UNKNOWN, "no evaluator for this requirement"))
            elif not check.ready:
                unmet.append(check)
        return unmet

    def highest_stage(self):
        """The best stage every requirement of which is met.

        Walks UP from OBSERVE and stops at the first stage that is not
        fully satisfied, so a later stage cannot be reached by skipping a
        blocked earlier one.
        """
        best = stages.STAGE_OBSERVE
        for stage in stages.STAGE_ORDER[1:]:
            if self.unmet_for(stage):
                break
            best = stage
        return best

    def as_dict(self) -> Dict[str, Any]:
        highest = self.highest_stage()
        return {
            "generated_at": self.generated_at,
            "checks": [check.as_dict() for check in self.checks],
            "highest_stage_permitted": highest,
            "unmet_for_next_stage": [
                check.as_dict() for check in
                self.unmet_for(stages.next_stage(highest) or highest)],
            "live_rollout": DISABLED,
        }


# ------------------------------------------------------- persisted state

def read_verification(conn, key) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        f"SELECT * FROM {STATE_TABLE} WHERE key = ?", (str(key),)).fetchone()
    return dict(row) if row is not None else None


def verification_status(conn, key) -> str:
    """UNVERIFIED unless something recorded otherwise."""
    row = read_verification(conn, key)
    return (row or {}).get("status") or UNVERIFIED


def record_verification(conn, key, status, *, detail="", evidence=None, now=None) -> None:
    """Write an observation. A recorded MISMATCH is never overwritten by
    a later READY -- see the module docstring on latching."""
    stamp = (now or datetime.now(timezone.utc)).isoformat() if not isinstance(now, str) else now
    existing = read_verification(conn, key)
    if existing and existing.get("status") == MISMATCH and status != MISMATCH:
        logger.warning(
            "refusing to clear a recorded %s for %s with %s; a valuation "
            "disagreement is not disproved by one clean read", MISMATCH, key, status)
        return
    if existing:
        conn.execute(
            f"UPDATE {STATE_TABLE} SET status = ?, detail = ?, observed_at = ?, "
            "evidence = ?, updated_at = ? WHERE key = ?",
            (status, detail, stamp, evidence, stamp, str(key)))
    else:
        conn.execute(
            f"INSERT INTO {STATE_TABLE} (key, status, detail, observed_at, evidence, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (str(key), status, detail, stamp, evidence, stamp, stamp))


# --------------------------------------------- position valuation check

#: How far the internally computed position value may sit from the
#: broker's own before it counts as a disagreement. Deliberately tight:
#: this is here to absorb decimal rounding, not to paper over a formula
#: that means something different. One cent, or one part per million on
#: a large position, whichever is larger.
VALUATION_ABS_TOLERANCE_USD = 0.01
VALUATION_REL_TOLERANCE = 1e-6


def compare_position_valuation(internal_usd, broker_usd) -> Dict[str, Any]:
    """Do the two valuations agree to rounding? Pure; records nothing."""
    for name, value in (("internal", internal_usd), ("broker", broker_usd)):
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            return {"status": UNVERIFIED, "detail": f"{name} valuation is not a number"}
    difference = abs(float(internal_usd) - float(broker_usd))
    scale = max(abs(float(broker_usd)), 1.0)
    tolerance = max(VALUATION_ABS_TOLERANCE_USD, VALUATION_REL_TOLERANCE * scale)
    agrees = difference <= tolerance
    return {
        "status": READY if agrees else MISMATCH,
        "difference_usd": round(difference, 6),
        "tolerance_usd": round(tolerance, 6),
        "detail": ("agrees within rounding tolerance" if agrees else
                   f"internal and broker valuations differ by ${difference:,.4f}, "
                   f"beyond the ${tolerance:,.4f} rounding tolerance"),
    }


# ------------------------------------------------------------ the matrix

def build_matrix(*, conn=None, risk_state=None, equity_snapshot=None,
                 candidate_source_ok=None, candidate_decision_enabled=None,
                 kill_switch_healthy=None, reconciliation_healthy=None,
                 minimum_order_verified=False, exit_policy_defined=False,
                 fees_reported=False, now=None) -> Matrix:
    """Every requirement, evaluated. Missing evidence is never READY."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    checks: List[Check] = []

    def add(key, ready, ready_detail="", not_ready_status=UNKNOWN, not_ready_detail=""):
        checks.append(Check(key, READY if ready else not_ready_status,
                            ready_detail if ready else not_ready_detail))

    # -- things this codebase controls -------------------------------------
    add(stages.REQ_CANDIDATE_DECISION_DISABLED,
        candidate_decision_enabled is False,
        "candidate_decision.enabled is false",
        BLOCKED,
        "the global Candidate Decision layer must stay disabled")
    add(stages.REQ_CANDIDATE_SOURCE, bool(candidate_source_ok),
        "a validated S1 candidate set is available",
        UNKNOWN, "no validated S1 candidate set")
    add(stages.REQ_WHOLE_SHARE, True,
        "domain.cash_sizing floors in Decimal; fractional orders are refused")
    add(stages.REQ_NO_DUPLICATE_ORDER, True,
        "execution.idempotency + open-order check + s1_live.reentry")

    # -- account facts ------------------------------------------------------
    cash_ok = bool(equity_snapshot is not None and equity_snapshot.cash_usd is not None)
    add(stages.REQ_ACCOUNT_CASH, cash_ok,
        "CTRP6504R output2[crcy_cd=USD].frcr_dncl_amt_2, cross-validated "
        "against get_orderable_usd",
        UNKNOWN, "no account-level USD cash figure")
    add(stages.REQ_ACCOUNT_EQUITY,
        bool(equity_snapshot is not None and equity_snapshot.available),
        "equity = verified USD cash + USD position value",
        UNKNOWN, (equity_snapshot.detail if equity_snapshot is not None
                  else "no equity snapshot"))

    start_known = bool(risk_state is not None and risk_state.start_equity is not None)
    add(stages.REQ_START_EQUITY, start_known,
        "captured pre-open and durable across restarts",
        UNKNOWN, "the day's starting equity has not been captured")
    peak_known = bool(risk_state is not None and risk_state.peak_equity is not None)
    add(stages.REQ_PEAK_EQUITY, peak_known,
        "cross-day high-water mark recorded",
        UNKNOWN, "no high-water mark recorded")

    daily_ok = bool(risk_state is not None and risk_state.daily_loss_status == "ALLOW")
    add(stages.REQ_DAILY_LOSS, daily_ok, "within the -2% daily limit",
        UNKNOWN, (risk_state.status_detail if risk_state is not None
                  else "daily loss not measured"))
    dd_ok = bool(risk_state is not None and risk_state.drawdown_status == "ALLOW")
    add(stages.REQ_DRAWDOWN, dd_ok, "within the -10% drawdown limit",
        UNKNOWN, (risk_state.status_detail if risk_state is not None
                  else "drawdown not measured"))

    # -- operational health -------------------------------------------------
    add(stages.REQ_KILL_SWITCH, bool(kill_switch_healthy),
        "no HALT and entries are permitted",
        BLOCKED, "the kill switch is engaged or unreadable")
    add(stages.REQ_RECONCILIATION, bool(reconciliation_healthy),
        "reconciliation is current",
        UNKNOWN, "reconciliation is stale, failing or unreadable")

    # -- open decisions -----------------------------------------------------
    add(stages.REQ_MINIMUM_ORDER, bool(minimum_order_verified),
        "the broker's real minimum order value is established",
        UNKNOWN,
        "DEFAULT_MIN_ORDER_AMOUNT_USD is a placeholder and must not be used "
        "as live grounds")
    add(stages.REQ_EXIT_POLICY, bool(exit_policy_defined),
        "an S1 exit policy is defined",
        BLOCKED,
        "no S1 exit policy exists; the only wired policy is the scalping one, "
        "whose horizon does not match a daily-bar trend signal")
    add(stages.REQ_FEES, bool(fees_reported),
        "broker-reported fees are available for net P&L",
        UNKNOWN, "fees are UNKNOWN, so net P&L stays NULL")

    # -- facts only a real order establishes --------------------------------
    for key in VERIFICATION_KEYS:
        status = verification_status(conn, key) if conn is not None else UNVERIFIED
        detail = {
            stages.REQ_POSITION_VALUATION:
                "needs one real position to compare against the broker's valuation",
            stages.REQ_RESERVED_ORDER_CASH:
                "needs one real resting order to observe orderable cash change",
        }[key]
        checks.append(Check(key, status,
                            "" if status == READY else detail))

    return Matrix(checks=checks, generated_at=stamp)


def format_matrix(matrix: Matrix) -> str:
    lines = ["S1 LIVE READINESS", f"  generated {matrix.generated_at}", ""]
    width = max(len(check.key) for check in matrix.checks)
    for check in matrix.checks:
        lines.append(f"  {check.key:{width}}  {check.status:11} {check.detail}")
    highest = matrix.highest_stage()
    lines.append("")
    lines.append(f"  live rollout            : {DISABLED}")
    lines.append(f"  highest stage permitted : {highest}")
    following = stages.next_stage(highest)
    if following:
        lines.append(f"  blocking {following}:")
        for check in matrix.unmet_for(following):
            lines.append(f"    - {check.key}: {check.status} ({check.detail})")
    return "\n".join(lines)
