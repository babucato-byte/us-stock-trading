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

#: Why the orderable-cash read ended the way it did. One NOT_MEASURED
#: covering all of these hid the difference between "KIS refused the
#: call" and "the account has no money", which need opposite responses.
ORDERABILITY_OK = "ORDERABILITY_OK"
ORDERABILITY_ZERO = "ORDERABILITY_ZERO"
ORDERABILITY_SESSION_UNAVAILABLE = "ORDERABILITY_SESSION_UNAVAILABLE"
ORDERABILITY_RATE_LIMITED = "ORDERABILITY_RATE_LIMITED"
ORDERABILITY_API_ERROR = "ORDERABILITY_API_ERROR"
ORDERABILITY_AUTH_ERROR = "ORDERABILITY_AUTH_ERROR"
ORDERABILITY_PARSE_ERROR = "ORDERABILITY_PARSE_ERROR"
ORDERABILITY_EXCHANGE_MAPPING_ERROR = "ORDERABILITY_EXCHANGE_MAPPING_ERROR"
ORDERABILITY_PRICE_INVALID = "ORDERABILITY_PRICE_INVALID"
ORDERABILITY_UNKNOWN = "ORDERABILITY_UNKNOWN"


def _orderability_reason(exc) -> str:
    """Classify a failed orderable-cash read from the exception itself."""
    name = type(exc).__name__
    text = str(exc).lower()
    if "unavailable" in name.lower() or "KISOrderableCashUnavailable" in name:
        return ORDERABILITY_PARSE_ERROR
    if "rate" in text or "limit" in text or "429" in text or "초당" in text:
        return ORDERABILITY_RATE_LIMITED
    if "token" in text or "auth" in text or "401" in text or "403" in text:
        return ORDERABILITY_AUTH_ERROR
    if "session" in text or "장운영" in text or "거래시간" in text:
        return ORDERABILITY_SESSION_UNAVAILABLE
    if "timeout" in text or "connection" in text or "http" in text \
            or "500" in text or "502" in text or "503" in text:
        return ORDERABILITY_API_ERROR
    if "Broker" in name or "KIS" in name:
        return ORDERABILITY_API_ERROR
    return ORDERABILITY_UNKNOWN


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


class ReportBrokerSnapshot:
    """The account-level KIS reads, taken ONCE for the whole report.

    Positions, open orders and the account snapshot do not vary by
    symbol, but they were read inside the candidate loop -- so a report
    over two symbols made them twice, each sweeping three exchanges, and
    a report over more made them more. Under that load KIS answered the
    orderable-amount call with a body carrying no `output`, and the gate
    honestly reported that it could not measure a question it had already
    answered correctly on a cold call in 0.2s.

    Failures are isolated per field. One unreadable list must not turn
    every gate into the same NOT_MEASURED: a failed open-order read says
    nothing about reconciliation, and neither says anything about whether
    KIS calls the symbol a common stock.

    Read-only by construction: it holds three lists and has no method
    that could place an order.
    """

    def __init__(self, broker=None):
        self.positions = None
        self.open_orders = None
        self.account = None
        self.errors: Dict[str, str] = {}
        self.calls: Dict[str, int] = {
            "positions_calls": 0, "open_orders_calls": 0,
            "account_snapshot_calls": 0, "orderable_usd_calls": 0,
            "price_detail_calls": 0,
        }
        self.fetched_at = datetime.now(timezone.utc).isoformat()
        if broker is None:
            return
        for field, method, counter in (
                ("positions", "get_positions", "positions_calls"),
                ("open_orders", "get_open_orders", "open_orders_calls"),
                ("account", "get_account_snapshot", "account_snapshot_calls")):
            # Resolved per field, inside the guard: a broker that does not
            # implement one of the three must cost that one field, not the
            # whole snapshot.
            try:
                call = getattr(broker, method)
                self.calls[counter] += 1
                setattr(self, field, call())
            except Exception as exc:  # noqa: BLE001 - isolated per field
                self.errors[field] = f"{type(exc).__name__}: {str(exc)[:140]}"
                logger.warning("S6 final check: %s read failed", field,
                               exc_info=True)

    def count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def as_dict(self) -> Dict[str, Any]:
        return {"fetched_at": self.fetched_at, "errors": dict(self.errors),
                "calls": dict(self.calls),
                "positions": (len(self.positions)
                              if self.positions is not None else None),
                "open_orders": (len(self.open_orders)
                                if self.open_orders is not None else None),
                "account_readable": self.account is not None}


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

    # The account-level reads, taken once, before the candidate loop --
    # so every symbol in the loop shares the same three answers instead
    # of asking KIS for them again.
    snapshot = ReportBrokerSnapshot(broker)

    try:
        report.update(_candidate_facts(
            day, resolved_session, variant, modes=modes, conn=conn,
            broker=broker, rollout=rollout, now=moment, snapshot=snapshot))
    except Exception as exc:  # noqa: BLE001
        logger.warning("S6 final check: candidate facts failed", exc_info=True)
        report["errors"].append(f"candidates: {exc}")
        report.setdefault("candidates", [])
        report.setdefault("candidate_count", None)
        report.setdefault("common_stock_count", None)
        report.setdefault("buy_gates", _unmeasured(f"report error: {exc}"))
        report.setdefault("submit_boundary_reached", False)

    # §6: the call counts are part of the report, not a log line. A
    # regression that reintroduces a per-symbol account read shows up
    # here as a number greater than one, which a test can assert on.
    report["broker_reads"] = snapshot.as_dict()

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

    # §2: the window facts an observation must check, carried on the
    # report so a supplier never has to re-derive them.
    from scanners.base import scan_window

    window = scan_window.evaluate()

    return {
        "calendar_trading_day": window.calendar_trading_day,
        "scan_allowed": window.scan_allowed,
        "scan_window_reason": window.reason,
        "scan_window_session": window.session,
        "scan_ran": ran,
        "regular_market_state": window.regular_market_state,
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
                     now, snapshot=None) -> Dict[str, Any]:
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

    # Gates are evaluated ONCE PER SYMBOL, not once per published row.
    #
    # The store is append-only, so fifteen scans of the same session hold
    # fifteen rows for the same two symbols. Evaluating per row made
    # fifteen times the necessary KIS calls -- the orderable-amount read,
    # the price-detail read and the open-order read, each rate-limited --
    # and the limiter then refused the later ones, so `cash_orderability`
    # and `kis_execution_sanity` came back NOT_MEASURED for candidates
    # that were perfectly answerable. A report that degrades its own
    # answers by asking too often is worse than a slow one.
    gate_cache: Dict[str, Dict[str, Dict[str, str]]] = {}

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
            "buy_gates": _cached_gates(
                gate_cache, symbol, offered=offered,
                qualification=qualification, conn=conn, broker=broker,
                rollout=rollout, session=session,
                blocked_before_gates=blocked_before_gates,
                price=row.get("price"), snapshot=snapshot),
        })
    facts["candidates"] = rows

    facts["buy_gates"] = _cycle_gates(rows, blocked_before_gates)

    # §6: the CANDIDATE's own quality and the SESSION's order policy are
    # different questions and are answered separately.
    #
    # S6-O's real candidates pass instrument, qualify, freshness,
    # reconciliation, duplicate and the KIS price sanity check. The only
    # thing refusing them is `risk_matrix`, because OVERNIGHT_DAYTIME is
    # REALTIME_SHADOW -- a statement about the SESSION, not about the
    # candidate. Letting that one policy answer erase the candidate
    # observation would discard evidence the pipeline genuinely produced.
    facts["common_stock_candidate_dry_run"] = _candidate_dry_run(rows)
    facts["order_policy_ready"] = _order_policy_ready(rows, session, modes)
    # The boundary is reached only when some candidate cleared the source,
    # qualified, and left no gate BLOCKing. NOT_MEASURED does not reach
    # it: an unasked gate is not a passed one.
    facts["submit_boundary_reached"] = any(
        row["source_verified"] and row["qualify_verified"]
        and all(gate["status"] == PASS for gate in row["buy_gates"].values())
        for row in rows)
    facts["broker_submit_count"] = 0
    return facts


#: Gates that describe the CANDIDATE. `risk_matrix` is absent on
#: purpose: it answers whether the SESSION may order, which is
#: `order_policy_ready`'s question.
CANDIDATE_GATES = ("instrument", "cash_orderability", "reconciliation",
                   "duplicate_protection", "kis_execution_sanity")


def _candidate_dry_run(rows) -> Dict[str, Any]:
    """Did a real COMMON_STOCK candidate clear every CANDIDATE gate?

    Order policy is deliberately excluded -- see `order_policy_ready`. A
    candidate that is a supported-exchange common stock, qualifies, is
    fresh, reconciles, is not a duplicate and sits inside KIS's own
    trading-day range has been fully observed, whether or not the session
    it arrived in may trade.
    """
    eligible = [r for r in rows
                if r.get("live_eligible") and r.get("qualify_verified")]
    if not eligible:
        return {"status": NOT_MEASURED, "symbols": [],
                "detail": "no COMMON_STOCK candidate qualified this cycle"}

    passed, blocked = [], []
    for row in eligible:
        gates = row.get("buy_gates") or {}
        verdicts = {g: (gates.get(g) or {}).get("status") for g in CANDIDATE_GATES}
        if all(v == PASS for v in verdicts.values()):
            passed.append(row["symbol"])
        else:
            blocked.append(
                f"{row['symbol']}: " + ", ".join(
                    f"{g}={v}" for g, v in verdicts.items() if v != PASS))
    if passed:
        return {"status": PASS, "symbols": passed,
                "detail": f"{len(passed)} candidate(s) cleared every "
                          f"candidate gate"}
    return {"status": NOT_MEASURED, "symbols": [],
            "detail": "; ".join(blocked[:3])}


def _order_policy_ready(rows, session, modes) -> Dict[str, Any]:
    """May this session actually place the order? Policy only.

    Separate from the candidate observation so a shadow session can
    record a fully-observed candidate AND an honest BLOCK at once.
    """
    from config import scanner_live_mode

    reasons = []
    if not s6_sessions.orders_allowed(session):
        reasons.append(f"session {session} is {s6_sessions.mode_for(session)}")
    if not scanner_live_mode.is_limited_live(s6_sessions.SCANNER_NAME, modes):
        reasons.append("S6 is DISCOVERY_ONLY")
    risk = [(r.get("buy_gates") or {}).get("risk_matrix", {}) for r in rows]
    blocked = [d.get("detail") for d in risk if d.get("status") == BLOCK]
    if blocked:
        reasons.append(blocked[0])
    if reasons:
        return {"status": BLOCK, "detail": "; ".join(dict.fromkeys(reasons))}
    return {"status": PASS, "detail": "session and live mode both permit orders"}


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


def _cached_gates(cache, symbol, **kw) -> Dict[str, Dict[str, str]]:
    """`_gates_for` at most once per symbol -- and only for a MISS.

    Written out rather than `cache.setdefault(symbol, _gates_for(...))`,
    which reads like a cache and is not one: Python evaluates the default
    argument before setdefault is entered, so every row paid for a full
    gate evaluation and the cache only ever discarded the result. That is
    what made a report over 32 rows for 2 symbols take 1060 seconds and
    hand KIS 32 orderable-amount reads, until the rate limiter answered
    with a body carrying no `output` and the gate honestly said
    NOT_MEASURED.
    """
    if symbol not in cache:
        cache[symbol] = _gates_for(symbol, **kw)
    return cache[symbol]


def _gates_for(symbol, *, offered, qualification, conn, broker, rollout,
               session, blocked_before_gates, price=None, snapshot=None
               ) -> Dict[str, Dict[str, str]]:
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

    # A caller that did not bring the report's snapshot gets its own, so
    # the gates are answered from the same three reads either way. The
    # report always brings one; this is the direct-call path.
    if snapshot is None:
        snapshot = ReportBrokerSnapshot(broker)

    gates["cash_orderability"] = _cash_gate(broker, symbol, price, snapshot)
    gates["reconciliation"] = _reconciliation_gate(conn, broker, snapshot)
    gates["duplicate_protection"] = _duplicate_gate(conn, symbol,
                                                    broker=broker,
                                                    snapshot=snapshot)
    gates["kis_execution_sanity"] = _execution_gate(broker, symbol, snapshot)
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


def _duplicate_gate(conn, symbol, *, broker, snapshot=None) -> Dict[str, str]:
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
    if snapshot is not None:
        if snapshot.errors.get("open_orders"):
            return _result(NOT_MEASURED,
                           f"open-order read failed: "
                           f"{snapshot.errors['open_orders']}")
        open_orders = snapshot.open_orders or []
    else:
        try:
            open_orders = broker.get_open_orders() or []
        except Exception as exc:  # noqa: BLE001
            return _result(NOT_MEASURED, f"open-order read failed: {exc}")
    clash = [o for o in open_orders
             if str(o.get("pdno") or o.get("PDNO") or "").upper() == symbol]
    if clash:
        return _result(BLOCK, f"{len(clash)} open broker order(s) for {symbol}")
    return _result(PASS, "no S6 position and no open broker order")


def _cash_gate(broker, symbol, price=None, snapshot=None) -> Dict[str, str]:
    """Orderable cash, read the way the BUY cycle reads it.

    `get_orderable_usd` is the inquire-psamount READ (TTTS3007R). It is
    not an order endpoint and submits nothing -- so it can be evaluated
    here for real, using the candidate's own published price as the limit
    KIS is asked about. This used to return NOT_MEASURED on the reasoning
    that the report "builds no order", which conflated building an order
    with asking a question about one.

    Without a price there is nothing to ask KIS about, and the account
    read alone is reported instead.
    """
    # The account read is the REPORT's, taken once. Re-reading it per
    # symbol is what amplified the call volume.
    if snapshot is not None:
        if snapshot.errors.get("account"):
            return _result(NOT_MEASURED,
                           f"{ORDERABILITY_API_ERROR}: account read failed: "
                           f"{snapshot.errors['account']}")
        if snapshot.account is None:
            return _result(NOT_MEASURED,
                           f"{ORDERABILITY_API_ERROR}: no account snapshot")

    limit = None
    try:
        limit = float(price) if price is not None else None
    except (TypeError, ValueError):
        limit = None
    if limit is None or limit <= 0 or limit != limit:
        return _result(NOT_MEASURED,
                       f"{ORDERABILITY_PRICE_INVALID}: no usable candidate "
                       f"price ({price!r}) to ask orderable cash about")

    try:
        from market_data.exchange_registry import build_kis_instrument

        instrument, _record = build_kis_instrument(symbol)
    except Exception as exc:  # noqa: BLE001
        return _result(BLOCK,
                       f"{ORDERABILITY_EXCHANGE_MAPPING_ERROR}: {str(exc)[:120]}")

    try:
        if snapshot is not None:
            snapshot.count("orderable_usd_calls")
        available = broker.get_orderable_usd(instrument, limit)
    except Exception as exc:  # noqa: BLE001 - an unanswered question is
        # never "no cash". Classified so a rate limit, an auth failure and
        # a genuinely empty account are three different findings.
        return _result(NOT_MEASURED,
                       f"{_orderability_reason(exc)}: {str(exc)[:140]}")

    if available is None:
        return _result(NOT_MEASURED,
                       f"{ORDERABILITY_PARSE_ERROR}: KIS returned no usable "
                       f"orderable amount")
    try:
        from domain.cash_sizing import whole_shares_affordable

        shares = whole_shares_affordable(available, limit)
    except Exception as exc:  # noqa: BLE001
        return _result(NOT_MEASURED,
                       f"{ORDERABILITY_PARSE_ERROR}: {str(exc)[:120]}")
    if shares < 1:
        # A real, parsed answer: there IS no money for a whole share.
        # That is a BLOCK, not an absence of measurement.
        return _result(BLOCK,
                       f"{ORDERABILITY_ZERO}: orderable {available:.2f} USD "
                       f"affords 0 whole shares at {limit:.2f}")
    return _result(PASS, f"{ORDERABILITY_OK}: orderable {available:.2f} USD "
                         f"affords {shares} whole share(s) at {limit:.2f}")


def _reconciliation_gate(conn, broker, snapshot=None) -> Dict[str, str]:
    if conn is None:
        return _result(NOT_MEASURED, "no database connection")
    if snapshot is not None and snapshot.errors.get("positions"):
        # Isolated: an unreadable position list says nothing about the
        # other gates, and they are answered independently.
        return _result(NOT_MEASURED,
                       f"position read failed: {snapshot.errors['positions']}")
    try:
        from reconciliation import internal_holdings

        positions = (snapshot.positions if snapshot is not None
                     else broker.get_positions()) or []
        account = [{"symbol": p.symbol, "venue": getattr(p, "venue", None),
                    "quantity": p.quantity} for p in positions]
        summary = internal_holdings.summary(conn, account)
    except Exception as exc:  # noqa: BLE001
        return _result(NOT_MEASURED, f"reconciliation unavailable: {exc}")
    if not summary.get("coverage_healthy"):
        return _result(BLOCK, f"coverage gaps: {summary.get('coverage_gaps')}")
    return _result(PASS, "; ".join(summary.get("attribution") or []))


def _execution_gate(broker, symbol, snapshot=None) -> Dict[str, str]:
    """The day-range execution-price check S6 now inherits as a strategy
    source. Before `STRATEGY_SOURCES` named S6, this candidate would have
    been given the legacy previous-close 0.30% check instead."""
    try:
        from s1_live import execution_price

        # `instrument=None` lets it resolve the KIS instrument the same
        # way it does for S1, rather than this report building a second
        # one that could disagree about the exchange.
        if snapshot is not None:
            snapshot.count("price_detail_calls")
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

    reads = report.get("broker_reads") or {}
    if reads.get("calls"):
        calls = reads["calls"]
        # Printed because the count IS the finding: an account call above
        # one means the loop started re-reading it again.
        lines += [
            "",
            f"  KIS reads        : positions={calls.get('positions_calls')} "
            f"open_orders={calls.get('open_orders_calls')} "
            f"account={calls.get('account_snapshot_calls')} "
            f"orderable={calls.get('orderable_usd_calls')} "
            f"price_detail={calls.get('price_detail_calls')}",
        ]
        if reads.get("errors"):
            lines.append(f"  KIS read errors  : {reads['errors']}")

    for row in report.get("candidates") or []:
        lines += [
            "",
            f"  -- {row['symbol']} (rank {_fmt(row.get('rank'))}, "
            f"score {_fmt(row.get('score'))})",
            f"     security type    : {_fmt(row.get('security_type'))} "
            f"(live_eligible={_fmt(row.get('live_eligible'))})",
            f"     generated at     : {_fmt(row.get('generated_at'))}",
            f"     observed at      : {_fmt(row.get('read_at'))}",
            f"     age at observation: {_fmt(row.get('candidate_age_seconds'))}s",
            f"     consumed at      : {_fmt(row.get('consumed_at'))}"
            f"   (live LIMITED_LIVE consumer)",
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
