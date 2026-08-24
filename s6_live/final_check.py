"""What an operator needs immediately before promoting S6-R. Read-only.

The question this answers
------------------------
"Is the REGULAR pipeline actually working end to end, right now" is
currently answered by reading a scanner log, a candidate file, a monitor
message, the position store and two reconciliation reports and holding
the result in your head. That is where a NOT_MEASURED becomes a PASS: not
through carelessness, but because six sources with different vocabularies
invite the reader to fill a gap with an assumption.

So this walks the real path -- the scan, the publication, the source, the
qualification, and each BUY gate in the order the shared cycle applies
them -- and prints what each step ACTUALLY answered.

It stops at the boundary
------------------------
`submit_boundary_reached` is the last thing evaluated, and it means
"a candidate got as far as the point where the execution engine would be
called". Nothing here calls it. There is no broker submission on any path
through this module, `broker_submit_count` is a constant 0, and
tests/test_s6_reports.py asserts both against this module's parsed import
and call graph rather than against this paragraph.

Three answers, not two
----------------------
Every gate is PASS, BLOCK or NOT_MEASURED, for the reason
`s6_live.readiness` makes the same distinction: a gate that could not be
asked -- no broker connection, no candidate, a session that refuses
orders -- has not passed. Most of them are NOT_MEASURED while S6 is
DISCOVERY_ONLY, and the report says so plainly instead of showing a row
of ticks that mean "we never got that far".

What it can answer without a broker
-----------------------------------
Two gates are genuinely offline and are evaluated even with no
connection: the COMMON_STOCK classification (KIS's master is an ingested
file) and the risk matrix (rollout limits and the position store). Those
are also the two that would silently mis-answer for S6 if the shared
cycle had not been told S6 is a strategy source -- see
`kis_live_trading.STRATEGY_SOURCES`.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import s6_sessions

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "s6_r_final_check_v1"

PASS = "PASS"
BLOCK = "BLOCK"
NOT_MEASURED = "NOT_MEASURED"

#: The gates, in the order `run_live_buy_entry_cycle` applies them. The
#: order is part of the report: a gate that never ran because an earlier
#: one blocked is a different fact from one that ran and passed.
GATES = (
    "instrument",
    "cash_orderability",
    "reconciliation",
    "duplicate_protection",
    "risk_matrix",
    "kis_execution_sanity",
)

#: Gates answerable from local state alone. Everything else needs a live
#: broker connection and is NOT_MEASURED without one.
OFFLINE_GATES = frozenset({"instrument", "risk_matrix"})


def _strategy_live_mode(modes=None) -> str:
    """Whether S6 ITSELF is promoted -- not what its session permits."""
    from config import scanner_live_mode

    table = modes if modes is not None else scanner_live_mode.SCANNER_LIVE_MODE
    return str(table.get(s6_sessions.SCANNER_NAME,
                         scanner_live_mode.MODE_DISCOVERY_ONLY))


def _strategy_is_live(modes=None) -> bool:
    from config import scanner_live_mode

    return scanner_live_mode.is_limited_live(s6_sessions.SCANNER_NAME, modes)


def _age(generated_at, read_at) -> Optional[float]:
    """Seconds between publication and this read. None if unmeasurable."""
    if not generated_at or read_at is None:
        return None
    try:
        from s1_live.freshness import as_utc

        made = as_utc(generated_at)
        if made is None:
            return None
        return round((read_at - made).total_seconds(), 3)
    except Exception:  # noqa: BLE001 - a measurement must not raise
        return None


def _observed_freshness(rows, read_at) -> Dict[str, Any]:
    """The NEWEST row's age at the moment this report read it.

    Newest, not first: a store appended by six scans holds six
    generations, and the age that matters is the freshest available --
    the one a consumer would act on.
    """
    stamps = [r.get("generated_at") for r in rows or [] if r.get("generated_at")]
    newest = max(stamps) if stamps else None
    return {"generated_at": newest,
            "read_at": read_at.isoformat() if read_at is not None else None,
            "age_seconds": _age(newest, read_at)}


def _result(status: str, detail: str = "") -> Dict[str, str]:
    return {"status": status, "detail": detail}


def _unmeasured(reason: str) -> Dict[str, Dict[str, str]]:
    return {gate: _result(NOT_MEASURED, reason) for gate in GATES}


def build(*, conn=None, broker=None, trading_day=None, session=None,
          modes=None, rollout=None, now=None) -> Dict[str, Any]:
    """The whole report. Never raises: a report that crashed reports nothing.

    `broker` is optional and read-only when supplied -- account, open
    orders and positions. Omitting it is the normal weekend case and
    produces NOT_MEASURED for the four gates that need it.
    """
    moment = now or datetime.now(timezone.utc)
    from market_hours import EASTERN, get_market_state, us_trading_day
    from scanners.base import scan_session
    from scanners.publish import s6_snapshot

    day = str(trading_day or us_trading_day(moment))
    resolved_session = (scan_session.normalize(session)
                        or scan_session.session_at(moment.astimezone(EASTERN)))
    market = get_market_state(moment)
    variant = s6_sessions.variant_for(resolved_session)

    report: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": moment.isoformat(),
        "origin": s6_snapshot.origin(),
        "trading_day": day,
        "session": resolved_session,
        "variant": variant,
        "market_state": market,
        # Capability and promotion, never merged into one word.
        #
        # `session_mode` is what THIS SESSION would permit; it reads
        # LIMITED_LIVE for REGULAR whether or not S6 is promoted, because
        # it describes the session. `strategy_live_mode` is whether S6
        # itself may act, and it is the one that is DISCOVERY_ONLY today.
        # Printed as `strategy_mode`, the session's answer said
        # "LIMITED_LIVE" on a report read immediately before deciding to
        # promote -- which is the misreading this whole file exists to
        # prevent, committed by the file itself.
        "session_mode": s6_sessions.mode_for(resolved_session),
        "strategy_live_mode": _strategy_live_mode(modes),
        "order_capable": s6_sessions.orders_allowed(resolved_session),
        "orders_allowed": (market != "CLOSED"
                           and s6_sessions.orders_allowed(resolved_session)
                           and _strategy_is_live(modes)),
        # The invariant, stated rather than computed: no path through
        # this module reaches a broker submission.
        "broker_submit_count": 0,
        "errors": [],
    }

    for name, step in (("scan", lambda: _scan_facts(day, resolved_session)),
                       ("market", lambda: _market_facts(market, resolved_session))):
        try:
            report.update(step())
        except Exception as exc:  # noqa: BLE001 - a report that cannot
            # measure something records that, and carries on measuring
            # the rest. Half a report beats a traceback.
            logger.warning("S6 final check: %s facts failed", name,
                           exc_info=True)
            report["errors"].append(f"{name}: {exc}")

    try:
        report.update(_candidate_facts(
            day, resolved_session, variant, modes=modes, conn=conn,
            broker=broker, rollout=rollout, now=moment))
    except Exception as exc:  # noqa: BLE001
        logger.warning("S6 final check: candidate facts failed", exc_info=True)
        report["errors"].append(f"candidates: {exc}")
        report.setdefault("candidates", [])
        report.setdefault("candidate_count", None)
        report.setdefault("common_stock_count", None)
        report.setdefault("buy_gates", _unmeasured(f"report error: {exc}"))
        report.setdefault("submit_boundary_reached", False)

    return report


def _market_facts(market: str, session: str) -> Dict[str, Any]:
    """Was the REGULAR market genuinely open at report time?

    A weekend produces False, not an absence -- and the activation
    evaluator reads the absence differently from the False, which is why
    the two are kept distinct all the way up.
    """
    return {
        "market_open_verified": bool(market == "REGULAR"
                                     and session == "REGULAR"),
        "market_open_detail": (f"market_state={market} session={session}"),
    }


def _scan_facts(day: str, session: str) -> Dict[str, Any]:
    """When the scan started, when it finished, and whether one ran."""
    from scanners.publish import candidates as publisher
    from scanners.publish import scan_cycle

    state = scan_cycle.state(day, session, scanner=s6_sessions.SCANNER_NAME)
    marker = scan_cycle.latest_run(day, session,
                                   strategy_id=s6_sessions.STRATEGY_ID) or {}
    ran = publisher.scan_ran(day, session)

    # Printed, because "which directory" is the failure this whole report
    # would otherwise miss. The scanner runtime and the trading runtime
    # resolve `candidate_dir()` from their own environments, and when
    # those disagree the producer writes a perfectly good file that the
    # consumer never sees -- no error on either side. That has already
    # happened once here, to S2. A reader comparing this line with the
    # scanner's own log answers it in a second instead of an hour.
    try:
        directory = str(publisher.candidate_dir())
    except Exception as exc:  # noqa: BLE001
        directory = f"unavailable: {exc}"

    published: List[Dict[str, Any]] = []
    publisher_error = None
    try:
        published = [r for r in publisher.read(day, session)
                     if str(r.get("strategy_id")) == s6_sessions.STRATEGY_ID]
    except Exception as exc:  # noqa: BLE001
        publisher_error = str(exc)

    return {
        "scanner_tick_verified": bool(ran and not state.running),
        "scan_in_progress": state.running,
        "scan_state": state.as_dict(),
        "scan_started_at": marker.get("started_at"),
        "scan_completed_at": marker.get("completed_at") or marker.get("marked_at"),
        "scan_duration_seconds": marker.get("duration_seconds"),
        "scan_run_id": marker.get("scanner_run_id"),
        "last_scan_status": marker.get("status"),
        # "The publisher wrote a file we can read", which is a different
        # claim from "the file has rows in it". A quiet session verifies
        # the publisher and finds nothing.
        "publisher_verified": bool(ran and publisher_error is None),
        "publisher_error": publisher_error,
        "published_rows": len(published),
        "candidate_dir": directory,
    }


def _candidate_facts(day, session, variant, *, modes, conn, broker, rollout,
                     now) -> Dict[str, Any]:
    """Every candidate, its provenance, and each BUY gate's answer."""
    from s6_live import candidate_source as source_module
    from s6_live import qualification as qualification_module
    from scanners.publish import candidates as publisher
    from scanners.publish import eligibility

    raw = [r for r in publisher.read(day, session)
           if str(r.get("strategy_id")) == s6_sessions.STRATEGY_ID
           and str(r.get("variant") or "") == variant]
    enriched = eligibility.enrich(raw)
    eligible = [r for r in enriched if r.get("live_eligible")]

    source = source_module.S6CandidateSource(
        trading_day=day, session=session, modes=modes)
    symbols = source.symbols()
    described = source.describe()
    live_freshness = source.freshness()

    # Freshness measured HERE, from the rows this report just read, and
    # NOT taken from the live source.
    #
    # The live source refuses at its mode gate while S6 is
    # DISCOVERY_ONLY, so `consumed_at` is None and the age is
    # unmeasurable -- which made the observation circular: consuming
    # needs LIMITED_LIVE, LIMITED_LIVE needs the observation, and the
    # observation needs consuming. That is a measurement problem, not a
    # reason to relax a gate.
    #
    # This read is genuine: the same shared-store rows, through the same
    # publisher, at a real moment. It carries NO order permission -- the
    # rows never reach a broker from here, and `source_verified` below
    # still reports the LIVE source's own answer, which stays False
    # until S6 is promoted. The two are different facts and both are
    # reported.
    read_at = now
    observed = _observed_freshness(enriched, read_at)

    facts: Dict[str, Any] = {
        "candidate_count": len(enriched),
        "common_stock_count": len(eligible),
        "source_symbols": symbols,
        "source_refusal": described.get("refusal"),
        # The observation-path measurement (always available).
        "candidate_generated_at": observed["generated_at"],
        "candidate_read_at": observed["read_at"],
        "candidate_age_at_read_seconds": observed["age_seconds"],
        # The LIVE consumer's own measurement (None until promoted).
        "candidate_consumed_at": live_freshness.get("candidate_consumed_at"),
        "candidate_age_at_consume_seconds": live_freshness.get(
            "candidate_age_at_consume_seconds"),
    }

    # A refusal at the SOURCE means nothing downstream was asked. Saying
    # so once, at the top, is the difference between "the gates passed"
    # and "the gates were never reached".
    blocked_before_gates = described.get("refusal")

    rows: List[Dict[str, Any]] = []
    for row in enriched:
        symbol = str(row.get("symbol") or "").upper()
        offered = symbol in {s.upper() for s in symbols}
        # Qualification is a PURE function of the published row -- it
        # takes no broker and no mode. Calling it directly measures what
        # the shared cycle WOULD decide, without needing the live source
        # to have offered the symbol first.
        qualification = qualification_module.qualify_s6(
            symbol, candidate_row=row)
        rows.append({
            "symbol": symbol,
            "rank": row.get("rank"),
            "score": row.get("score"),
            "security_type": row.get("security_type"),
            "live_eligible": bool(row.get("live_eligible")),
            "generated_at": row.get("generated_at"),
            "read_at": observed["read_at"],
            "candidate_age_seconds": _age(row.get("generated_at"), read_at),
            "consumed_at": live_freshness.get("candidate_consumed_at"),
            "source_verified": offered,
            "source_detail": (None if offered else
                              (blocked_before_gates
                               or "not offered by the S6 candidate source")),
            "qualify_verified": (bool(qualification.qualified)
                                 if qualification is not None else None),
            "qualify_detail": (
                (qualification.reason_code or qualification.detail)
                if qualification is not None and not qualification.qualified
                else None),
            "buy_gates": _gates_for(
                symbol, offered=offered, qualification=qualification,
                conn=conn, broker=broker, rollout=rollout, session=session,
                blocked_before_gates=blocked_before_gates),
        })
    facts["candidates"] = rows

    facts["buy_gates"] = _cycle_gates(rows, blocked_before_gates)
    # The boundary is reached only when some candidate cleared the source,
    # qualified, and left no gate BLOCKing. NOT_MEASURED does not reach
    # it: an unasked gate is not a passed one.
    facts["submit_boundary_reached"] = any(
        row["source_verified"] and row["qualify_verified"]
        and all(gate["status"] == PASS for gate in row["buy_gates"].values())
        for row in rows)
    facts["broker_submit_count"] = 0
    return facts


def _cycle_gates(rows, blocked_before_gates) -> Dict[str, Dict[str, str]]:
    """The per-gate summary across candidates.

    PASS only when at least one candidate actually passed it; BLOCK when
    every candidate that reached it was refused; NOT_MEASURED when none
    reached it at all. Aggregating any other way would let "no candidate"
    read as "no problem".
    """
    if not rows:
        return _unmeasured(blocked_before_gates or "no S6 candidate this cycle")

    summary = {}
    for gate in GATES:
        statuses = [row["buy_gates"][gate]["status"] for row in rows]
        details = [row["buy_gates"][gate]["detail"] for row in rows
                   if row["buy_gates"][gate]["detail"]]
        if PASS in statuses:
            summary[gate] = _result(
                PASS, f"{statuses.count(PASS)} of {len(statuses)} candidate(s)")
        elif BLOCK in statuses:
            summary[gate] = _result(BLOCK, "; ".join(details[:3]))
        else:
            summary[gate] = _result(NOT_MEASURED, "; ".join(details[:3]))
    return summary


def _gates_for(symbol, *, offered, qualification, conn, broker, rollout,
               session, blocked_before_gates) -> Dict[str, Dict[str, str]]:
    """One candidate through each gate, in cycle order.

    The offline gates are evaluated even for a candidate the source
    refused: "would KIS call this a common stock" is answerable and worth
    knowing on a day when nothing could trade, and it is the single fact
    `common_stock_dry_run_verified` rests on.
    """
    gates = _unmeasured(blocked_before_gates
                        or "the shared BUY cycle was not reached")

    gates["instrument"] = _instrument_gate(symbol)
    gates["risk_matrix"] = _risk_matrix_gate(conn, rollout=rollout,
                                             session=session)

    if broker is None:
        for gate in GATES:
            if gate not in OFFLINE_GATES:
                gates[gate] = _result(
                    NOT_MEASURED,
                    "no broker connection was supplied to this report")
        gates["duplicate_protection"] = _duplicate_gate(conn, symbol,
                                                        broker=None)
        return gates

    gates["cash_orderability"] = _cash_gate(broker, symbol)
    gates["reconciliation"] = _reconciliation_gate(conn, broker)
    gates["duplicate_protection"] = _duplicate_gate(conn, symbol, broker=broker)
    gates["kis_execution_sanity"] = _execution_gate(broker, symbol)
    return gates


def _instrument_gate(symbol) -> Dict[str, str]:
    """KIS's own master, asked exactly as the BUY cycle asks it."""
    from s1_live import security_type

    try:
        verdict = security_type.require_live_eligible(symbol)
    except security_type.SecurityTypeUnavailable as exc:
        text = str(exc)
        # A missing or stale master is not a verdict about the symbol --
        # it is the absence of one, and the two must not read alike.
        if text.startswith((security_type.REASON_CACHE_UNAVAILABLE,
                            security_type.REASON_CACHE_STALE,
                            security_type.REASON_CACHE_WRONG_SOURCE)):
            return _result(NOT_MEASURED, text)
        return _result(BLOCK, text)
    except Exception as exc:  # noqa: BLE001
        return _result(NOT_MEASURED, f"classification failed: {exc}")
    return _result(PASS, f"{verdict.security_type} on {verdict.exchange} "
                         f"(master {verdict.asof})")


def _risk_matrix_gate(conn, *, rollout, session) -> Dict[str, str]:
    """Rollout limits, the session matrix and the S6 position count."""
    from config.live_rollout_config import LiveRolloutConfig

    try:
        config = rollout or LiveRolloutConfig.from_env()
        config.validate()
    except Exception as exc:  # noqa: BLE001
        return _result(BLOCK, f"live_rollout config invalid: {exc}")

    if not s6_sessions.orders_allowed(session):
        return _result(BLOCK,
                       f"session {session} is "
                       f"{s6_sessions.mode_for(session)}; orders are enabled "
                       f"only in {sorted(s6_sessions.LIVE_SESSIONS)}")
    if not config.enabled:
        return _result(BLOCK, "live_rollout.enabled is False")

    if conn is None:
        return _result(NOT_MEASURED,
                       "no database connection: the open-position count "
                       "could not be read")
    try:
        from s6_live import position_store

        held = position_store.open_count(conn)
    except Exception as exc:  # noqa: BLE001
        return _result(NOT_MEASURED, f"position count unavailable: {exc}")

    if held >= config.max_open_positions:
        return _result(BLOCK, f"{held} S6 position(s) already count against "
                              f"max_open_positions={config.max_open_positions}")
    return _result(PASS, f"qty<={config.max_quantity_per_order} "
                         f"positions={held}/{config.max_open_positions}")


def _duplicate_gate(conn, symbol, *, broker) -> Dict[str, str]:
    """Both halves: our own store, and the broker's open orders.

    PASS requires both. The internal half alone is the half that survives
    a restart badly -- an order submitted and not yet recorded lives only
    at the broker -- so answering from it alone would be the optimistic
    half of the question.
    """
    internal = None
    if conn is not None:
        try:
            from s6_live import position_store

            internal = position_store.load_by_symbol(conn, symbol)
        except Exception as exc:  # noqa: BLE001
            return _result(NOT_MEASURED, f"position store unreadable: {exc}")
        if internal is not None:
            return _result(BLOCK, f"S6 already holds {symbol} "
                                  f"({internal.get('status')})")

    if broker is None:
        return _result(NOT_MEASURED,
                       "the internal store shows no S6 position for this "
                       "symbol; the broker's open orders were not read"
                       if conn is not None else
                       "neither the internal store nor the broker was read")
    try:
        open_orders = broker.get_open_orders() or []
    except Exception as exc:  # noqa: BLE001
        return _result(NOT_MEASURED, f"open-order read failed: {exc}")
    clash = [o for o in open_orders
             if str(o.get("pdno") or o.get("PDNO") or "").upper() == symbol]
    if clash:
        return _result(BLOCK, f"{len(clash)} open broker order(s) for {symbol}")
    return _result(PASS, "no S6 position and no open broker order")


def _cash_gate(broker, symbol) -> Dict[str, str]:
    """Orderable cash, read the way the BUY cycle reads it.

    Deliberately NOT a sizing decision. The cycle asks KIS per (symbol,
    exchange, limit price) at the exact price the intent is built with,
    and this report has no intent -- so it reports the account read
    succeeding or failing and stops there rather than inventing a price
    to ask about.
    """
    try:
        snapshot = broker.get_account_snapshot()
    except Exception as exc:  # noqa: BLE001
        return _result(BLOCK, f"KIS account read failed: {exc}")
    if snapshot is None:
        return _result(BLOCK, "KIS returned no account snapshot")
    return _result(NOT_MEASURED,
                   "the account is readable; orderable cash is asked per "
                   "(symbol, exchange, limit price) at order time and this "
                   "report builds no order")


def _reconciliation_gate(conn, broker) -> Dict[str, str]:
    if conn is None:
        return _result(NOT_MEASURED, "no database connection")
    try:
        from reconciliation import internal_holdings

        positions = broker.get_positions() or []
        account = [{"symbol": p.symbol, "venue": getattr(p, "venue", None),
                    "quantity": p.quantity} for p in positions]
        summary = internal_holdings.summary(conn, account)
    except Exception as exc:  # noqa: BLE001
        return _result(NOT_MEASURED, f"reconciliation unavailable: {exc}")
    if not summary.get("coverage_healthy"):
        return _result(BLOCK, f"coverage gaps: {summary.get('coverage_gaps')}")
    return _result(PASS, "; ".join(summary.get("attribution") or []))


def _execution_gate(broker, symbol) -> Dict[str, str]:
    """The day-range execution-price check S6 now inherits as a strategy
    source. Before `STRATEGY_SOURCES` named S6, this candidate would have
    been given the legacy previous-close 0.30% check instead."""
    try:
        from s1_live import execution_price

        # `instrument=None` lets it resolve the KIS instrument the same
        # way it does for S1, rather than this report building a second
        # one that could disagree about the exchange.
        verdict = execution_price.evaluate_symbol(symbol, broker=broker)
    except Exception as exc:  # noqa: BLE001
        return _result(NOT_MEASURED, f"execution-price check unavailable: {exc}")
    if not verdict.passed:
        return _result(BLOCK, f"{verdict.reason_code}: {verdict.detail}")
    return _result(PASS, verdict.detail or "inside the trading-day range")


def attach_to_snapshots(report: Dict[str, Any]) -> int:
    """Write this report's per-candidate gate answers onto the snapshots.

    §3's snapshot is written by the scanner runtime, which cannot ask a
    BUY gate. This is the other half: the same candidate, now carrying
    what the trading side found. Returns how many snapshot rows were
    amended.

    Append-only, like everything else that records a fact here: the
    amendment is a NEW row referencing the original rather than an edit
    of it, so the scanner's untouched observation stays on disk.
    """
    from scanners.publish import s6_snapshot

    rows = [r for r in report.get("candidates") or [] if r.get("live_eligible")]
    if not rows:
        return 0

    existing = s6_snapshot.read(trading_day=report.get("trading_day"),
                                variant=s6_snapshot.VARIANT_REGULAR)
    by_symbol = {str(r.get("symbol") or "").upper(): r for r in existing}

    written = 0
    for row in rows:
        original = by_symbol.get(row["symbol"])
        if original is None:
            continue
        amended = dict(original)
        amended.update({
            "schema": s6_snapshot.SCHEMA_VERSION,
            "recorded_at": report.get("generated_at"),
            "origin": report.get("origin"),
            "amends": original.get("recorded_at"),
            "consumed_at": row.get("consumed_at"),
            "candidate_age_seconds": row.get("candidate_age_seconds"),
            "qualify_result": (
                s6_snapshot.NOT_MEASURED if row.get("qualify_verified") is None
                else (PASS if row["qualify_verified"] else BLOCK)),
            "buy_gates": {gate: result["status"]
                          for gate, result in (row.get("buy_gates") or {}).items()},
            "buy_gate_detail": {gate: result["detail"]
                                for gate, result in (row.get("buy_gates") or {}).items()},
            "submit_boundary_reached": report.get("submit_boundary_reached"),
            "broker_submit_count": 0,
        })
        if s6_snapshot.append(amended):
            written += 1
    return written


def _fmt(value, dash="-") -> str:
    return dash if value is None else str(value)


def format_report(report: Dict[str, Any]) -> str:
    """The operator-facing rendering. Same facts, no new ones."""
    lines = [
        "S6-R FINAL CHECK",
        "=" * 64,
        f"  generated        : {_fmt(report.get('generated_at'))}",
        f"  origin           : {_fmt(report.get('origin'))}",
        f"  trading day      : {_fmt(report.get('trading_day'))}",
        f"  session/variant  : {_fmt(report.get('session'))} / "
        f"{_fmt(report.get('variant'))}",
        f"  market state     : {_fmt(report.get('market_state'))}",
        f"  session mode     : {_fmt(report.get('session_mode'))}"
        f"   (what this SESSION permits)",
        f"  strategy mode    : {_fmt(report.get('strategy_live_mode'))}"
        f"   (whether S6 ITSELF may act)",
        f"  order capable    : {_fmt(report.get('order_capable'))}",
        f"  orders allowed   : {_fmt(report.get('orders_allowed'))}",
        "",
        f"  market_open_verified  : {_fmt(report.get('market_open_verified'))}"
        f"   ({_fmt(report.get('market_open_detail'))})",
        f"  scanner_tick_verified : {_fmt(report.get('scanner_tick_verified'))}",
        f"  scan started          : {_fmt(report.get('scan_started_at'))}",
        f"  scan completed        : {_fmt(report.get('scan_completed_at'))}",
        f"  scan duration (s)     : {_fmt(report.get('scan_duration_seconds'))}",
        f"  scan in progress      : {_fmt(report.get('scan_in_progress'))}",
        f"  last scan status      : {_fmt(report.get('last_scan_status'))}",
        f"  publisher_verified    : {_fmt(report.get('publisher_verified'))}",
        f"  candidate directory   : {_fmt(report.get('candidate_dir'))}",
        "",
        f"  candidate age@read (s) : "
        f"{_fmt(report.get('candidate_age_at_read_seconds'))}",
        f"  candidate age@consume  : "
        f"{_fmt(report.get('candidate_age_at_consume_seconds'))}"
        f"   (live consumer; None until promoted)",
        "",
        f"  candidates       : {_fmt(report.get('candidate_count'))}",
        f"  COMMON_STOCK     : {_fmt(report.get('common_stock_count'))}",
    ]
    if report.get("source_refusal"):
        lines.append(f"  source refusal   : {report['source_refusal']}")

    for row in report.get("candidates") or []:
        lines += [
            "",
            f"  -- {row['symbol']} (rank {_fmt(row.get('rank'))}, "
            f"score {_fmt(row.get('score'))})",
            f"     security type    : {_fmt(row.get('security_type'))} "
            f"(live_eligible={_fmt(row.get('live_eligible'))})",
            f"     generated at     : {_fmt(row.get('generated_at'))}",
            f"     read at          : {_fmt(row.get('read_at'))}",
            f"     consumed at      : {_fmt(row.get('consumed_at'))}",
            f"     age at consume   : {_fmt(row.get('candidate_age_seconds'))}s",
            f"     source_verified  : {_fmt(row.get('source_verified'))}"
            + (f"  ({row['source_detail']})" if row.get("source_detail") else ""),
            f"     qualify_verified : {_fmt(row.get('qualify_verified'))}"
            + (f"  ({row['qualify_detail']})" if row.get("qualify_detail") else ""),
        ]
        for gate in GATES:
            result = (row.get("buy_gates") or {}).get(gate) or {}
            lines.append(f"     {gate:<21}: {_fmt(result.get('status')):<12} "
                         f"{result.get('detail') or ''}")

    lines += [
        "",
        "  BUY gate summary",
        "  " + "-" * 62,
    ]
    for gate in GATES:
        result = (report.get("buy_gates") or {}).get(gate) or {}
        lines.append(f"    {gate:<23}: {_fmt(result.get('status')):<12} "
                     f"{result.get('detail') or ''}")

    lines += [
        "",
        f"  submit_boundary_reached : {_fmt(report.get('submit_boundary_reached'))}",
        f"  broker submit count     : {report.get('broker_submit_count', 0)}",
    ]
    for error in report.get("errors") or []:
        lines.append(f"  ERROR: {error}")
    return "\n".join(lines)
