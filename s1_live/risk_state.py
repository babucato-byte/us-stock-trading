"""Durable account risk state: start-of-day equity, peak equity, verdicts.

Why start equity is written once and read forever
-------------------------------------------------
The daily loss limit is measured against the equity the day STARTED
from. If a process that restarts at 14:00 re-read equity and called it
the day's start, a -2% day would silently become a fresh -2% of room --
and it would do so at exactly the moment a restart is most likely, which
is after something went wrong. So the row is written at most once per
ET trading day, and every later refresh reads it back.

`capture_start_of_day()` therefore refuses to overwrite. It also refuses
to CREATE a start row once the regular session is already under way:
PHASE 4B §5 is explicit that an afternoon restart must not adopt the
current figure as the start, and there is no source in this codebase
from which the true 09:30 equity could be reconstructed after the fact.
The honest result is UNKNOWN, and UNKNOWN blocks.

Peak equity, and what it cannot see
-----------------------------------
The peak is a single account-level high-water mark. It rises when equity
exceeds it and never falls, which is what makes a drawdown measurable
across days rather than resetting with the calendar.

It cannot distinguish a deposit from a profit. This broker wrapper
implements six endpoints -- price, balance, orderable amount, order,
cancel, unfilled and fills -- and none of them reports an external cash
movement. So a $500 deposit raises equity, raises the peak, and the
drawdown that follows is then measured against a peak that includes
capital the strategy never earned. That UNDERSTATES the strategy's own
drawdown, which is the unsafe direction.

`record_peak()` therefore flags any rise larger than
`EXTERNAL_FLOW_SUSPECT_PCT` as `external_flow_suspected` rather than
silently accepting it. The flag does not block by itself -- a genuinely
good day can clear it -- but it is recorded so an operator can correct
the baseline, and it is reported. This limit is stated rather than
engineered around because engineering around it would mean inferring
cash flows from data that does not contain them.

Everything gates NEW entries only
---------------------------------
No function here is consulted on the sell side. A drawdown limit that
blocked liquidation would trap the account in the position the limit
exists to escape.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import market_hours
from s1_live import equity as equity_module
from s1_live import risk_guards

logger = logging.getLogger(__name__)

ALLOW = risk_guards.ALLOW
BLOCK = risk_guards.BLOCK
UNKNOWN = risk_guards.UNKNOWN

SOURCE_BROKER = "kis_balance"
SOURCE_PRE_OPEN = "pre_open_capture"
SOURCE_CARRIED = "carried_forward"

REASON_NO_START_EQUITY = "START_EQUITY_UNKNOWN"
REASON_LATE_FIRST_CAPTURE = "START_EQUITY_LATE_FIRST_CAPTURE"
REASON_NO_PEAK = "PEAK_EQUITY_UNKNOWN"
REASON_STALE = "EQUITY_SNAPSHOT_STALE"

#: A single refresh raising the peak by more than this fraction is
#: flagged as possibly an external deposit rather than trading profit.
#: Not a threshold that blocks -- a marker that something needs an
#: operator's eye. Deliberately loose: it is a smell test, not a
#: measurement, and this system cannot measure the thing it is about.
EXTERNAL_FLOW_SUSPECT_PCT = 0.20

#: Sessions in which a FIRST start-equity capture is accepted. Outside
#: these, a missing start row stays missing -- see the module docstring.
PRE_OPEN_SESSIONS = frozenset({"closed", "premarket"})


@dataclass
class RiskState:
    trading_day: str
    start_equity: Optional[float] = None
    start_equity_source: Optional[str] = None
    start_equity_captured_at: Optional[str] = None
    current_equity: Optional[float] = None
    current_equity_source: Optional[str] = None
    current_equity_as_of: Optional[str] = None
    peak_equity: Optional[float] = None
    peak_updated_at: Optional[str] = None
    external_flow_suspected: bool = False
    equity_currency: str = equity_module.USD
    daily_return_pct: Optional[float] = None
    drawdown_pct: Optional[float] = None
    daily_loss_status: str = UNKNOWN
    drawdown_status: str = UNKNOWN
    status_detail: str = ""
    last_successful_refresh: Optional[str] = None

    @property
    def entries_allowed(self) -> bool:
        return self.daily_loss_status == ALLOW and self.drawdown_status == ALLOW

    def as_dict(self) -> Dict[str, Any]:
        payload = dict(vars(self))
        payload["entries_allowed"] = self.entries_allowed
        return payload


def _now_iso(now=None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _session(now=None) -> str:
    try:
        return market_hours.get_us_market_session(now)
    except Exception:  # noqa: BLE001 - an unreadable calendar is not "pre-open"
        logger.warning("market session could not be determined", exc_info=True)
        return "unknown"


# --------------------------------------------------------------- peak

def read_peak(conn) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM s1_risk_peak WHERE id = 1").fetchone()
    return dict(row) if row is not None else None


def record_peak(conn, equity_usd, *, now=None, source=SOURCE_BROKER,
                currency=equity_module.USD) -> Dict[str, Any]:
    """Raise the high-water mark if this equity exceeds it.

    Never lowers it. A peak that fell with equity would make every
    drawdown zero, which is the most dangerous wrong answer this
    particular measure can give.
    """
    equity_module.assert_single_currency(currency)
    stamp = _now_iso(now)
    existing = read_peak(conn)

    if existing is None:
        conn.execute(
            "INSERT INTO s1_risk_peak (id, peak_equity, equity_currency, "
            "peak_updated_at, peak_source, external_flow_suspected, created_at, updated_at) "
            "VALUES (1,?,?,?,?,0,?,?)",
            (float(equity_usd), currency, stamp, source, stamp, stamp))
        return read_peak(conn)

    previous = float(existing["peak_equity"])
    if float(equity_usd) <= previous:
        return existing

    suspected = int(bool(
        previous > 0 and (float(equity_usd) - previous) / previous > EXTERNAL_FLOW_SUSPECT_PCT))
    if suspected:
        logger.warning(
            "peak equity rose %.1f%% in one refresh (%.2f -> %.2f); this system cannot "
            "distinguish a deposit from a profit, flagging for operator review",
            (float(equity_usd) - previous) / previous * 100.0, previous, float(equity_usd))
    conn.execute(
        "UPDATE s1_risk_peak SET peak_equity = ?, peak_updated_at = ?, peak_source = ?, "
        "external_flow_suspected = ?, updated_at = ? WHERE id = 1",
        (float(equity_usd), stamp, source,
         suspected or int(existing["external_flow_suspected"]), stamp))
    return read_peak(conn)


# ---------------------------------------------------------------- day

def read_day(conn, trading_day) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM s1_risk_state WHERE trading_day = ?", (str(trading_day),)).fetchone()
    return dict(row) if row is not None else None


def capture_start_of_day(conn, trading_day, snapshot: "equity_module.EquitySnapshot",
                         *, now=None, allow_late_capture=False) -> Dict[str, Any]:
    """Establish the day's starting equity, once.

    Returns a dict with `captured` (bool) and `reason`. An existing row
    is ALWAYS reused -- restart safety is the whole point.
    """
    stamp = _now_iso(now)
    existing = read_day(conn, trading_day)
    if existing is not None and existing.get("start_equity") is not None:
        return {"captured": False, "reason": "already recorded for this trading day",
                "state": existing}

    if not snapshot.available:
        _upsert_day(conn, trading_day, stamp, daily_loss_status=UNKNOWN,
                    status_detail=f"start equity not captured: {snapshot.detail}")
        return {"captured": False, "reason": snapshot.reason_code or REASON_NO_START_EQUITY,
                "state": read_day(conn, trading_day)}

    session = _session(now)
    if session not in PRE_OPEN_SESSIONS and not allow_late_capture:
        # §5: an afternoon restart must not adopt the current figure as
        # the day's start, and nothing here can reconstruct the real one.
        _upsert_day(conn, trading_day, stamp, daily_loss_status=UNKNOWN,
                    status_detail="no start equity for this day and the session is "
                                  f"already {session}; refusing to adopt the current "
                                  "figure as the day's starting equity")
        logger.warning("start equity missing for %s and the session is %s -- new S1 "
                       "entries stay blocked", trading_day, session)
        return {"captured": False, "reason": REASON_LATE_FIRST_CAPTURE,
                "state": read_day(conn, trading_day)}

    source = SOURCE_PRE_OPEN if session in PRE_OPEN_SESSIONS else "late_capture_override"
    _upsert_day(conn, trading_day, stamp,
                start_equity=snapshot.require(), start_equity_source=source,
                start_equity_captured_at=snapshot.as_of.isoformat(),
                equity_currency=snapshot.currency)
    # The day's opening equity is a known equity value and must take part
    # in the high-water mark. Without this the first peak the system ever
    # records is whatever the first REFRESH happened to observe -- and if
    # that refresh lands mid-drawdown, the peak is seeded below the real
    # high and the drawdown reads as ~0, which is the most dangerous
    # wrong answer this particular measure can give.
    #
    # It does not fix the deeper limitation: the very first peak this
    # system ever records is a floor, not a true historical high, because
    # nothing here knows what the account was worth before it started
    # looking. That is stated rather than papered over.
    record_peak(conn, snapshot.require(), now=now, source=source,
                currency=snapshot.currency)
    logger.info("start-of-day equity for %s captured at %s: %.2f (%s)",
                trading_day, snapshot.as_of.isoformat(), snapshot.require(), source)
    return {"captured": True, "reason": None, "state": read_day(conn, trading_day)}


def _upsert_day(conn, trading_day, stamp, **columns) -> None:
    existing = read_day(conn, trading_day)
    if existing is None:
        conn.execute(
            "INSERT INTO s1_risk_state (trading_day, created_at, updated_at) VALUES (?,?,?)",
            (str(trading_day), stamp, stamp))
    if not columns:
        conn.execute("UPDATE s1_risk_state SET updated_at = ? WHERE trading_day = ?",
                     (stamp, str(trading_day)))
        return
    assignments = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(
        f"UPDATE s1_risk_state SET {assignments}, updated_at = ? WHERE trading_day = ?",
        list(columns.values()) + [stamp, str(trading_day)])


# ------------------------------------------------------------ refresh

def refresh(conn, trading_day, snapshot: "equity_module.EquitySnapshot", *,
            now=None, max_age_seconds: Optional[float] = None,
            allow_late_capture=False) -> RiskState:
    """Fold one equity reading into the durable state and re-evaluate.

    Called on every S1 entry cycle. Returns the full state, whose
    `entries_allowed` is the single answer the cycle needs.
    """
    stamp = _now_iso(now)
    current = now or datetime.now(timezone.utc)

    capture_start_of_day(conn, trading_day, snapshot, now=now,
                         allow_late_capture=allow_late_capture)

    if not snapshot.available:
        _upsert_day(conn, trading_day, stamp, daily_loss_status=UNKNOWN,
                    drawdown_status=UNKNOWN,
                    status_detail=f"equity unavailable: {snapshot.detail}")
        return _load(conn, trading_day, detail=snapshot.detail)

    age = (current - snapshot.as_of).total_seconds()
    if max_age_seconds is not None and age > float(max_age_seconds):
        detail = (f"the equity snapshot is {age:.0f}s old, beyond the configured "
                  f"{float(max_age_seconds):.0f}s")
        _upsert_day(conn, trading_day, stamp, daily_loss_status=UNKNOWN,
                    drawdown_status=UNKNOWN, status_detail=detail)
        logger.warning("stale equity snapshot for %s: %s", trading_day, detail)
        return _load(conn, trading_day, detail=detail)

    record_peak(conn, snapshot.require(), now=now, currency=snapshot.currency)
    _upsert_day(conn, trading_day, stamp,
                current_equity=snapshot.require(),
                current_equity_source=snapshot.source,
                current_equity_as_of=snapshot.as_of.isoformat(),
                equity_currency=snapshot.currency,
                last_successful_refresh=stamp)

    return evaluate(conn, trading_day, now=now)


def evaluate(conn, trading_day, *, now=None) -> RiskState:
    """Re-derive both verdicts from stored state and persist them."""
    state = _load(conn, trading_day)
    stamp = _now_iso(now)

    daily = risk_guards.check_daily_loss(
        pnl_today_usd=(None if state.current_equity is None or state.start_equity is None
                       else state.current_equity - state.start_equity),
        basis_equity_usd=state.start_equity)
    drawdown = risk_guards.check_drawdown(
        equity_usd=state.current_equity, peak_equity_usd=state.peak_equity)

    details = [item.detail for item in (daily, drawdown) if item.detail]
    _upsert_day(conn, trading_day, stamp,
                daily_return_pct=daily.measured, drawdown_pct=drawdown.measured,
                daily_loss_status=daily.verdict, drawdown_status=drawdown.verdict,
                status_detail=" | ".join(details))
    return _load(conn, trading_day)


def _load(conn, trading_day, *, detail=None) -> RiskState:
    row = read_day(conn, trading_day) or {}
    peak = read_peak(conn) or {}
    return RiskState(
        trading_day=str(trading_day),
        start_equity=row.get("start_equity"),
        start_equity_source=row.get("start_equity_source"),
        start_equity_captured_at=row.get("start_equity_captured_at"),
        current_equity=row.get("current_equity"),
        current_equity_source=row.get("current_equity_source"),
        current_equity_as_of=row.get("current_equity_as_of"),
        peak_equity=peak.get("peak_equity"),
        peak_updated_at=peak.get("peak_updated_at"),
        external_flow_suspected=bool(peak.get("external_flow_suspected", 0)),
        equity_currency=row.get("equity_currency") or equity_module.USD,
        daily_return_pct=row.get("daily_return_pct"),
        drawdown_pct=row.get("drawdown_pct"),
        daily_loss_status=row.get("daily_loss_status") or UNKNOWN,
        drawdown_status=row.get("drawdown_status") or UNKNOWN,
        status_detail=detail or row.get("status_detail") or "",
        last_successful_refresh=row.get("last_successful_refresh"),
    )


def current_state(conn, trading_day) -> RiskState:
    """Read without refreshing -- for reporting and for the dry run."""
    return _load(conn, trading_day)
