"""What S6 did in a session it is not allowed to trade. Read-only.

Why the shadow sessions get their own report
--------------------------------------------
S6-O, S6-P and S6-A run the same scanner as S6-R and place no orders.
That combination is exactly the one that decays quietly: nothing fails,
nothing alerts, and six weeks later the shadow dataset turns out to have
a hole in it because the range never formed, or the publisher wrote to a
directory nobody read, or the variant on the rows said S6-R. Each of
those is invisible in a channel message that says "후보 수: 0", because a
broken session and a quiet one produce the same number.

So the report states the whole chain -- scan, range, publication,
runtime, reconciliation, freshness -- and states the expected values
alongside the observed ones. A reader compares two columns instead of
remembering what OVERNIGHT_DAYTIME is supposed to look like.

The expectations are derived, not typed in
------------------------------------------
`orders_allowed`, `order_capable` and the mode come from
`config.s6_sessions`, which is the same module the executor honours. If
somebody widens `LIVE_SESSIONS`, this report starts expecting orders in
that session rather than continuing to assert the old answer and reading
as a failure. A report that carried its own copy of the policy would be
a second policy.

It cannot trade and cannot promote
----------------------------------
No broker submission on any path; `broker_submit_count` is a constant 0.
Nothing here writes `scanner_live_mode` or `LIVE_SESSIONS`.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from config import s6_sessions

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "s6_session_report_v1"

OK = "OK"
DEGRADED = "DEGRADED"
NOT_MEASURED = "NOT_MEASURED"

#: What a healthy non-REGULAR S6 session looks like. Compared against,
#: never assumed -- see the module docstring on why these are computed
#: from `s6_sessions` rather than written down.
SHADOW_MODE = s6_sessions.MODE_REALTIME_SHADOW



def _session_orders_allowed(moment) -> bool:
    """Whether KIS is running an order-capable window at `moment`.

    Asked of `config.session_capability`, which is what the order path
    asks. A report that answered this itself would drift from what can
    actually be sent -- and it did, understating capability for every
    session but REGULAR.
    """
    from config import session_capability

    return bool(session_capability.capability_at(moment).orders_allowed)

def build(*, conn=None, trading_day=None, session=None, now=None,
          runtime_report=None, modes=None) -> Dict[str, Any]:
    """The report for one session. Never raises.

    `runtime_report` is the dict `scripts/run_s6_runtime.py` produced on
    this tick, when the caller has one. Without it the runtime row is
    NOT_MEASURED rather than assumed healthy -- a tick that did not run
    and a tick that ran cleanly are different facts.

    `modes` overrides the scanner live-mode table, exactly as
    `s6_live.final_check.build` already allowed. Promotion and session
    capability are independent, and a report that could only ever be
    read against the deployed table could not demonstrate that.
    """
    moment = now or datetime.now(timezone.utc)
    from market_hours import EASTERN, get_market_state, us_trading_day
    from scanners.base import scan_session
    from scanners.publish import s6_snapshot

    day = str(trading_day or us_trading_day(moment))
    resolved = (scan_session.normalize(session)
                or scan_session.session_at(moment.astimezone(EASTERN)))
    market = get_market_state(moment)
    variant = s6_sessions.variant_for(resolved)

    report: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": moment.isoformat(),
        "origin": s6_snapshot.origin(),
        "trading_day": day,
        "session": resolved,
        "variant": variant or None,
        "market_state": market,
        # Capability and promotion, kept apart. `session_mode` describes
        # what THIS SESSION permits; `strategy_live_mode` is whether S6
        # itself may act. One word for both would let a shadow session
        # under a promoted strategy read the same as a promoted session
        # under a shadow strategy.
        "session_mode": s6_sessions.mode_for(resolved),
        "strategy_live_mode": _strategy_live_mode(modes),
        "order_capable": s6_sessions.orders_allowed(resolved),
        # NOT `market != "CLOSED"`. `get_market_state()` reports the
        # US venue's own state and is CLOSED for the whole daytime
        # window by construction, so conjoining it here made the
        # report say "no orders" for a session KIS was running --
        # the same defect the runtime carried, in the place an
        # operator reads. Capability comes from the one resolver the
        # ORDER PATH uses, so a report cannot disagree with it.
        "orders_allowed": (_session_orders_allowed(moment)
                           and s6_sessions.orders_allowed(resolved)
                           and _strategy_is_live(modes)),
        "order_route_verified": scan_session.order_route_verified(resolved),
        "broker_submit_count": 0,
        "errors": [],
    }
    report["expected"] = _expectations(resolved)

    for name, step in (
        ("scan", lambda: _scan(day, resolved, now=moment)),
        ("candidates", lambda: _candidates(day, resolved, variant)),
        ("runtime", lambda: _runtime(runtime_report)),
        ("reconciliation", lambda: _reconciliation(conn)),
        ("slack", _slack),
    ):
        try:
            report.update(step())
        except Exception as exc:  # noqa: BLE001 - one section failing must
            # not cost the others; a partial report is still a report.
            logger.warning("S6 session report: %s failed", name, exc_info=True)
            report["errors"].append(f"{name}: {exc}")
            report.setdefault(f"{name}_status", NOT_MEASURED)

    report["matches_expectations"] = _compare(report)
    return report


def _strategy_live_mode(modes=None) -> str:
    """Whether S6 ITSELF is promoted -- not what its session permits."""
    from config import scanner_live_mode

    table = modes if modes is not None else scanner_live_mode.SCANNER_LIVE_MODE
    return str(table.get(s6_sessions.SCANNER_NAME,
                         scanner_live_mode.MODE_DISCOVERY_ONLY))


def _strategy_is_live(modes=None) -> bool:
    from config import scanner_live_mode

    return scanner_live_mode.is_limited_live(s6_sessions.SCANNER_NAME, modes)


def _expectations(session: str) -> Dict[str, Any]:
    """Read from the session matrix, so the two cannot disagree."""
    return {
        "variant": s6_sessions.variant_for(session) or None,
        "session_mode": s6_sessions.mode_for(session),
        "order_capable": s6_sessions.orders_allowed(session),
        "broker_submit_count": 0,
    }


def _compare(report: Dict[str, Any]) -> Dict[str, Any]:
    """Observed against expected, field by field.

    Reported as a per-field mapping rather than one boolean: "the session
    did not look right" sends a reader back to six log sources, which is
    the thing this file exists to stop.
    """
    expected = report.get("expected") or {}
    mismatches = {key: {"expected": value, "observed": report.get(key)}
                  for key, value in expected.items()
                  if report.get(key) != value}
    return {"matched": not mismatches, "mismatches": mismatches}


#: §9's three answers, kept apart. Merging them is how "the window did
#: not exist" gets read as "the producer is broken".
NOT_APPLICABLE = "NOT_APPLICABLE"
PRODUCER_MISSING = "PRODUCER_MISSING"
CLEAN_ZERO = "CLEAN_ZERO"


def _scan(day: str, session: str, *, now=None) -> Dict[str, Any]:
    from scanners.base import scan_window
    from scanners.publish import candidates as publisher
    from scanners.publish import scan_cycle

    state = scan_cycle.state(day, session, scanner=s6_sessions.SCANNER_NAME)
    marker = scan_cycle.latest_run(day, session,
                                   strategy_id=s6_sessions.STRATEGY_ID) or {}
    ran = publisher.scan_ran(day, session)
    # Evaluated at the report's OWN moment, not at wall-clock time.
    #
    # It took no argument, so a report built for a given instant
    # described a different instant's calendar and market state. Any
    # replayed or historical report carried today's calendar, and the
    # observation tests silently changed answer when a run crossed
    # midnight Eastern -- passing on a weekday and failing on the
    # weekend for a report whose own `now` was neither.
    window = scan_window.evaluate(now)

    status = NOT_MEASURED
    if ran:
        status = (DEGRADED if marker.get("status") == scan_cycle.STATUS_FAILED
                  else OK)

    # §9: a window that does not exist is NOT a missing producer.
    if not window.scan_allowed:
        producer = NOT_APPLICABLE
    elif not ran:
        producer = PRODUCER_MISSING
    elif marker.get("status") == scan_cycle.STATUS_FAILED:
        producer = DEGRADED
    else:
        producer = OK

    return {
        # §8: these are DIFFERENT facts and never share a field.
        "calendar_trading_day": window.calendar_trading_day,
        "scan_session": window.session,
        "session_date": (window.session_date.isoformat()
                         if window.session_date else None),
        "scan_allowed": window.scan_allowed,
        "scan_window_reason": window.reason,
        "regular_market_state": window.regular_market_state,
        "producer_status": producer,
        "scan_status": status,
        "scan_ran": ran,
        "scan_in_progress": state.running,
        "scan_started_at": marker.get("started_at"),
        "scan_completed_at": marker.get("completed_at") or marker.get("marked_at"),
        "scan_duration_seconds": marker.get("duration_seconds"),
        "scan_run_id": marker.get("scanner_run_id"),
        "last_scan_status": marker.get("status"),
    }


def _candidates(day: str, session: str, variant: str) -> Dict[str, Any]:
    """Counts, and whether a RANGE was actually formed.

    `range_ready` is the one that would otherwise go unnoticed: a session
    whose opening range never formed produces zero candidates and looks
    identical to a session where nothing broke out. It is read off the
    rows the scanner published, because the range is what produced them.
    """
    from scanners.publish import candidates as publisher
    from scanners.publish import eligibility

    rows = [r for r in publisher.read(day, session)
            if str(r.get("strategy_id")) == s6_sessions.STRATEGY_ID]
    mine = [r for r in rows if str(r.get("variant") or "") == (variant or "")]
    foreign = [r for r in rows if r not in mine]

    enriched = eligibility.enrich(mine) if mine else []
    live = [r for r in enriched if r.get("live_eligible")]

    ranges = [r.get("range_minutes") for r in mine
              if r.get("range_high") is not None]
    generated = [r.get("generated_at") for r in mine if r.get("generated_at")]

    return {
        "publisher_status": OK if rows or publisher.scan_ran(day, session)
        else NOT_MEASURED,
        "candidate_count": len(mine),
        "observed_count": len(mine),
        "live_eligible_count": len(live),
        "foreign_variant_rows": len(foreign),
        "range_ready": bool(ranges),
        "range_minutes": sorted({int(m) for m in ranges if m is not None}) or None,
        "shadow_range_minutes_under_comparison": list(
            s6_sessions.SHADOW_RANGE_MINUTES),
        "candidate_generated_at": max(generated) if generated else None,
        "freshness_status": OK if generated else NOT_MEASURED,
    }


def _runtime(runtime_report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not runtime_report:
        return {"runtime_status": NOT_MEASURED,
                "runtime_detail": "no runtime tick was supplied to this report"}
    status = runtime_report.get("status")
    return {
        "runtime_status": (OK if status in ("OK", "NO_S6_POSITIONS")
                           else DEGRADED),
        "runtime_detail": status,
        "runtime_errors": list(runtime_report.get("errors") or []),
        "runtime_buy_fills": len(runtime_report.get("buy_fills") or []),
        "runtime_exits": len(runtime_report.get("exits") or []),
        "runtime_retried": len(runtime_report.get("retried") or []),
        "runtime_sell_fills": len(runtime_report.get("sell_fills") or []),
    }


def _reconciliation(conn) -> Dict[str, Any]:
    if conn is None:
        return {"reconciliation_status": NOT_MEASURED,
                "reconciliation_detail": "no database connection"}
    try:
        from reconciliation import internal_holdings

        attribution = internal_holdings.attribution(conn)
    except Exception as exc:  # noqa: BLE001
        return {"reconciliation_status": NOT_MEASURED,
                "reconciliation_detail": f"unavailable: {exc}"}
    return {"reconciliation_status": OK,
            "reconciliation_detail": "; ".join(attribution)}


def _slack() -> Dict[str, Any]:
    """Whether the monitor channel is configured. Nothing is SENT here.

    A report that posted itself would make "was the channel reachable"
    depend on running the report, which is the wrong direction: the
    channel's health is a fact about the deployment.
    """
    try:
        from scanners.notify import monitor

        configured = monitor.webhook_configured()
    except Exception as exc:  # noqa: BLE001
        return {"slack_status": NOT_MEASURED, "slack_detail": str(exc)[:200]}
    return {"slack_status": OK if configured else DEGRADED,
            "slack_detail": ("monitor webhook configured" if configured
                             else "no monitor webhook is configured")}


def _fmt(value, dash="-") -> str:
    return dash if value is None else str(value)


def format_report(report: Dict[str, Any]) -> str:
    variant = report.get("variant") or "(none)"
    lines = [
        f"S6 SESSION REPORT -- {variant}",
        "=" * 64,
        f"  generated        : {_fmt(report.get('generated_at'))}",
        f"  origin           : {_fmt(report.get('origin'))}",
        f"  trading day      : {_fmt(report.get('trading_day'))}",
        f"  session          : {_fmt(report.get('session'))}",
        f"  market state     : {_fmt(report.get('market_state'))}",
        "",
        "  observed vs expected",
        "  " + "-" * 62,
    ]
    for key in ("variant", "session_mode", "order_capable",
                "broker_submit_count"):
        expected = (report.get("expected") or {}).get(key)
        observed = report.get(key)
        mark = "ok " if observed == expected else "!! "
        lines.append(f"    {mark}{key:<24}: {_fmt(observed):<20} "
                     f"expected {_fmt(expected)}")
    lines += [
        f"    strategy_live_mode      : {_fmt(report.get('strategy_live_mode'))}",
        f"    orders_allowed          : {_fmt(report.get('orders_allowed'))}",
        f"    order_route_verified    : {_fmt(report.get('order_route_verified'))}",
        "",
        "  pipeline",
        "  " + "-" * 62,
        f"    calendar trading day    : {_fmt(report.get('calendar_trading_day'))}",
        f"    scan session / date     : {_fmt(report.get('scan_session'))} / "
        f"{_fmt(report.get('session_date'))}",
        f"    scan_allowed            : {_fmt(report.get('scan_allowed'))} "
        f"({_fmt(report.get('scan_window_reason'))})",
        f"    regular_market_state    : {_fmt(report.get('regular_market_state'))}",
        f"    producer                : {_fmt(report.get('producer_status'))}",
        f"    scan                    : {_fmt(report.get('scan_status'))} "
        f"(ran={_fmt(report.get('scan_ran'))} "
        f"in_progress={_fmt(report.get('scan_in_progress'))})",
        f"    scan started            : {_fmt(report.get('scan_started_at'))}",
        f"    scan completed          : {_fmt(report.get('scan_completed_at'))}",
        f"    scan duration (s)       : {_fmt(report.get('scan_duration_seconds'))}",
        f"    range_ready             : {_fmt(report.get('range_ready'))} "
        f"minutes={_fmt(report.get('range_minutes'))}",
        f"    shadow ranges compared  : "
        f"{_fmt(report.get('shadow_range_minutes_under_comparison'))}",
        f"    publisher               : {_fmt(report.get('publisher_status'))}",
        f"    candidates (observed)   : {_fmt(report.get('observed_count'))}",
        f"    live eligible           : {_fmt(report.get('live_eligible_count'))}",
        f"    foreign-variant rows    : {_fmt(report.get('foreign_variant_rows'))}",
        f"    freshness               : {_fmt(report.get('freshness_status'))} "
        f"generated={_fmt(report.get('candidate_generated_at'))}",
        f"    runtime                 : {_fmt(report.get('runtime_status'))} "
        f"({_fmt(report.get('runtime_detail'))})",
        f"    reconciliation          : {_fmt(report.get('reconciliation_status'))} "
        f"{report.get('reconciliation_detail') or ''}",
        f"    slack                   : {_fmt(report.get('slack_status'))} "
        f"({_fmt(report.get('slack_detail'))})",
        "",
        f"  broker submit count : {report.get('broker_submit_count', 0)}",
    ]
    match = report.get("matches_expectations") or {}
    lines.append(f"  matches expectations: {_fmt(match.get('matched'))}")
    for key, pair in (match.get("mismatches") or {}).items():
        lines.append(f"    MISMATCH {key}: observed {pair['observed']!r}, "
                     f"expected {pair['expected']!r}")
    for error in report.get("errors") or []:
        lines.append(f"  ERROR: {error}")
    return "\n".join(lines)
