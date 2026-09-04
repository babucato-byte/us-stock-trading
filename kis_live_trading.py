"""KIS live-order buy-entry pipeline -- the actual cutover entrypoint
spec §1/§25 describes: Alpaca-sourced candidates/signals -> KIS price/
account re-validation -> Sizing -> central Order Gate -> Execution
Engine -> KIS. This module is NEW (not a modification of
paper_strategy_order.py) so the existing, extensively-tested Alpaca
paper order path stays completely untouched -- exactly the same
"grandfathered legacy path, new path is additive" pattern this
codebase's CODEX-040 cycle already established for the Alpaca-internal
live-entry pipeline.

Candidate/score discovery deliberately REUSES paper_strategy_order.
load_watchlist()/analyze_stock() (spec §5: 기존 기능 재사용) -- this
module adds nothing to how candidates are found or scored, only to what
happens to a qualifying candidate afterward.

SCOPE NOTE (documented, not silently omitted): this module implements
the BUY-entry path only. Sell-side automation (stop-loss/take-profit/
partial-exit/trailing-stop/time-exit/EOD-exit monitoring against live
KIS positions) is NOT implemented here -- spec's second message
describing that full strategy lifecycle was truncated mid-transmission
(see docs/autonomous/DECISION_LOG.md's KIS migration section) and
implementing stop-loss/exit logic by guessing at the missing
specification would be exactly the kind of safety-critical guess this
project's conventions forbid. `execution_engine.submit_sell_order()`
and `order_gate.evaluate_sell_gate()` already exist and are fully
tested (tests/test_execution_engine_kis.py, tests/test_order_gate.py)
-- only the strategy-side "when should we sell" decision logic and its
wiring into this pipeline remain.

SECURITY TYPE (was a residual risk, now enforced): `_build_instrument()`
below still defaults leveraged/inverse/otc to False, so on its own it
cannot tell a common stock from an ETF -- universe.csv carries no such
field. That used to mean the pipeline relied ENTIRELY on the operator
curating `live_rollout.allowed_symbols`, which a full-universe S1 scan
showed to be a real exposure: it returned IUSV, KBE, MILN, BLCV, LEMB,
IVOV, HYGV, JPIE and JPLD alongside the equities.

`s1_live/security_type.py` now classifies from KIS's own published
overseas master (field 9: 1=Index 2=Stock 3=ETP 4=Warrant), and the
per-symbol loop below calls `require_live_eligible()` before an order is
built. Only COMMON_STOCK on NASDAQ/NYSE/AMEX passes; ETP is refused
whole, and a symbol absent from the master is UNKNOWN and refused. The
operator list remains available as an additional restriction, but is no
longer the only thing preventing an ETF purchase.
See docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md.
"""

import logging
import os
import uuid
from datetime import datetime, timezone

import kis_position_manager
import paper_strategy_order as pso
import risk_config
import shadow_audit
import shadow_mode
from brokers.kis_broker import (
    KISAmbiguousResponseError,
    KISBrokerError,
    KISOrderableCashUnavailableError,
)
from config import scalping_strategy_v1_config as strat_cfg
from config.live_rollout_config import LiveRolloutConfig, LiveRolloutConfigError
from domain.cash_sizing import (
    INSUFFICIENT_CASH,
    ORDERABLE_CASH_UNAVAILABLE,
    whole_shares_affordable,
)
from domain.instrument import Instrument, InstrumentError, build_instrument
from domain.order_intent import OrderIntent, OrderIntentError
from domain.signal import Signal, SignalError, build_signal
from execution import entry_limits, execution_engine, idempotency, order_gate
from execution.execution_lock import ExecutionLockUnavailable
from execution import execution_lock
from market_data.exchange_registry import (
    ExchangeResolutionError,
    build_kis_instrument,
)
from execution.execution_engine import ExecutionEngineError
from execution.order_repository import FatalRepositoryConnectionError
from market_data.kis_validation_provider import (
    KISValidationProvider,
    compute_price_deviation_percent,
)
from market_data.base import MarketDataProviderError
from market_hours import EASTERN, us_trading_day
from operations import kill_switch as ops_kill_switch
from s1_live import candidate_source as s1_candidate_source
from s2_live import candidate_source as s2_candidate_source
from s6_live import candidate_source as s6_candidate_source
from s6_live import entry_lifecycle as s6_entry_lifecycle
from scanners.base import scan_session
from config import s6_sessions, session_capability, strategy_entry_policy
from s1_live import execution_price as s1_execution_price
from s1_live import security_type as s1_security_type
from state_store import db as state_db

logger = logging.getLogger(__name__)

SCORE_THRESHOLD = 70  # matches paper_strategy_order.py's existing threshold
SIGNAL_VALID_SECONDS = 120


class KISLiveTradingError(Exception):
    """Raised when a structural precondition (config, commit match) is
    invalid before any per-symbol processing even begins."""


_CYCLE_LEVEL_SYMBOL = "__CYCLE__"


def _record_slippage(*, now, **fields):
    """Record what an order cost against what it was meant to cost.

    Observation only. It writes one line to a JSONL file beside the
    shared state and returns whatever happens -- no order-DB write, no
    broker call, no bearing on any execution decision. A failure here
    must never cost a trade, so every failure is swallowed.

    The fill price is deliberately absent: at this point the broker has
    ACCEPTED, not filled. Reconciliation supplies the fill later, and
    recording the limit price as a fill would report zero slippage on
    every order.
    """
    try:
        from s6_live import slippage_log

        trading_day = now.strftime("%Y-%m-%d")
        slippage_log.append(slippage_log.build_record(now=now, **fields),
                            trading_day=trading_day)
    except Exception:  # noqa: BLE001
        logger.warning("could not record slippage for %s",
                       fields.get("symbol"), exc_info=True)


def _persist_blocked_record(*, symbol, side="buy", strategy_id="PAPER_STRATEGY_ORDER_SCORE_V1",
                             signal_price=None, kis_price=None, price_diff_percent=None,
                             planned_quantity=None, planned_limit_price=None, stop_price=None,
                             target_price=None, risk_gate_result, rejection_reason,
                             account_available_usd=None, existing_position_quantity=None,
                             existing_open_order=False, now):
    """Shadow Mode completeness (CODEX-review MEDIUM finding): every
    category the pipeline can block/skip on -- config block, signal
    expiry, symbol block, price deviation, insufficient balance,
    reconciliation failure, UNKNOWN present, duplicate order, HALT,
    Order Gate rejection -- must produce a durable Shadow Mode record,
    not just the subset that happened to already have a fully-built
    signal/order_intent in scope. This helper is deliberately tolerant
    of missing data (every non-required field defaults to None) so it
    can be called from the earliest possible point in the pipeline --
    including cycle-level structural blocks (HALT, config invalid, ...)
    that occur before any symbol/signal is ever built."""
    shadow_mode.persist(shadow_mode.build_record(
        signal_id=f"{symbol}-{now.isoformat()}", strategy_id=strategy_id, strategy_version="v1",
        code_commit=os.environ.get("DEPLOYED_COMMIT") or "", symbol=symbol, side=side,
        alpaca_signal_price=signal_price, kis_validation_price=kis_price,
        price_difference_percent=price_diff_percent, planned_quantity=planned_quantity,
        planned_limit_price=planned_limit_price, stop_price=stop_price, target_price=target_price,
        risk_gate_result=risk_gate_result, rejection_reason=rejection_reason,
        account_available_usd=account_available_usd,
        existing_position_quantity=existing_position_quantity,
        existing_open_order=existing_open_order, now=now,
    ))


def _audit(run_id, event_type, result, *, symbol=None, signal_id=None, internal_order_id=None,
            reason_code=None, detail=None, now):
    """CODEX-048: one durable audit row per evaluation step, in SQLite,
    for BOTH the block and the approve paths. `detail` is free text from
    an underlying exception and is redacted at the store boundary.

    Fails CLOSED: if the event cannot be persisted, shadow_audit.
    handle_audit_failure() retries the terminal SHADOW_ERROR, alerts, and
    raises ShadowAuditFailure -- the evaluation is abandoned rather than
    continuing with an incomplete audit trail."""
    try:
        shadow_audit.record_event(
            shadow_run_id=run_id, event_type=event_type, result=result, symbol=symbol,
            side="buy", signal_id=signal_id, internal_order_id=internal_order_id,
            reason_code=reason_code, payload={"detail": detail} if detail else None, now=now,
        )
    except shadow_audit.ShadowAuditError as exc:
        shadow_audit.handle_audit_failure(
            exc, shadow_run_id=run_id, symbol=symbol, side="buy", stage=event_type,
        )


def _finalize(run_id, outcome, *, symbol, now):
    """CODEX-053: terminal events go through shadow_audit.finalize_audit_run(),
    which is idempotent for the same event and refuses a conflicting one --
    so a run cannot end twice however many code paths think they own it."""
    try:
        shadow_audit.finalize_audit_run(
            audit_run_id=run_id,
            terminal_event=shadow_audit.terminal_event_for(outcome["result"]),
            internal_order_id=outcome.get("internal_order_id"), action="buy", symbol=symbol,
            side="buy", reason_code=outcome["reason_code"],
            payload={"detail": outcome["detail"]} if outcome.get("detail") else None, now=now,
        )
    except shadow_audit.ShadowAuditError as exc:
        shadow_audit.handle_audit_failure(
            exc, shadow_run_id=run_id, symbol=symbol, side="buy", stage="terminal",
        )


def _record_reentry_block(conn, exc, *, signal, symbol, order_intent, now):
    """Note a BUY refused as a same-day re-entry, and why it was ranked.

    Never fatal, and deliberately narrow: only this one reason code is
    recorded, because only this one is a policy choice worth auditing
    against what the price did next.
    """
    from execution import reentry_policy

    if getattr(exc, "reason_code", None) != reentry_policy.SAME_DAY_REENTRY_BLOCK:
        return
    try:
        strategy_id = getattr(order_intent, "strategy_id", None) or getattr(
            signal, "strategy_id", None)
        previous = reentry_policy.exits_today(
            conn, strategy_id=strategy_id, now=now).get(str(symbol).upper(), {})
        reentry_policy.record_block(
            conn, strategy_id=strategy_id, symbol=symbol,
            previous_exit=previous, now=now,
            candidate_rank=getattr(signal, "rank", None),
            candidate_score=getattr(signal, "score", None),
            candidate_price=getattr(order_intent, "limit_price", None))
    except Exception:  # noqa: BLE001 - the block already happened
        logger.warning("could not record the re-entry block for %s", symbol,
                       exc_info=True)


def _audit_cycle_block(run_id, event_type, reason_code, detail, *, now):
    """A cycle-level structural block. Emits the specific block event AND
    exactly one terminal SHADOW_BLOCKED, so no run is ever left without a
    final outcome event."""
    _audit(run_id, event_type, shadow_audit.RESULT_BLOCKED, symbol=_CYCLE_LEVEL_SYMBOL,
           reason_code=reason_code, detail=detail, now=now)
    _finalize(run_id, {"result": shadow_audit.RESULT_BLOCKED, "reason_code": reason_code,
                       "detail": detail}, symbol=_CYCLE_LEVEL_SYMBOL, now=now)


def _build_instrument(symbol, allowed_symbols):
    """See module docstring's RESIDUAL RISK note. `symbol` must already
    be on `allowed_symbols` (checked by the caller before this is
    invoked) -- leveraged/inverse/otc are trusted-False for exactly that
    reason, not independently detected.

    HIGH-1: the venue is RESOLVED, never assumed. This used to hardcode
    NASDAQ, which made every NYSE/AMEX name unpriceable (KIS answers a
    wrong-exchange quote with rt_cd=0 and an empty price). An unresolved
    symbol raises, so the caller blocks with EXCHANGE_UNKNOWN and places
    no order."""
    instrument, _record = build_kis_instrument(symbol)
    return instrument


def _get_deployed_commit():
    return os.environ.get("DEPLOYED_COMMIT", "")


def _get_validated_commit():
    return os.environ.get("VALIDATED_COMMIT", "")


def _get_allowed_account_no():
    return os.environ.get("KIS_ALLOWED_ACCOUNT_NO", "")


#: Sources that represent a LIVE STRATEGY, as opposed to the legacy
#: operator watchlist.
#:
#: The distinction matters because two gates below were written for the
#: strategy path and skipped for the legacy one: the COMMON_STOCK
#: classification and the KIS day-range execution-price check. Legacy
#: keeps its original behaviour deliberately -- it ships with an
#: operator-curated allow-list and the 0.30% deviation check, and
#: changing that was never in scope.
#:
#: They used to be keyed on "is this S1", which was the same thing as
#: "is this a strategy" until S2 existed. Left that way, S2 would have
#: reached a real order WITHOUT the COMMON_STOCK gate and with the
#: legacy 0.30% price check instead of the day-range one -- a strategy
#: silently acquiring weaker protection than the strategy it was
#: modelled on.
#:
#: S6 was in exactly that state until now. Its source, store, exits and
#: reconciliation were all wired while this set still named two
#: strategies, so the day S6 was promoted to LIMITED_LIVE its candidates
#: would have been handed the LEGACY path: no COMMON_STOCK
#: classification, and the previous-close 0.30% deviation check instead
#: of the day-range one. That is the same defect the paragraph above
#: describes, and it was invisible for the same reason -- nothing fails
#: while the strategy is DISCOVERY_ONLY, because no candidate of its
#: ever reaches this line.
#:
#: Membership here is not permission to trade. It selects which GATES
#: apply, and every one it selects is stricter than the alternative.
#: Whether S6 may order at all is `scanner_live_mode` and
#: `s6_sessions.orders_allowed`, neither of which this touches.
STRATEGY_SOURCES = frozenset({
    s1_candidate_source.SOURCE_S1,
    s2_candidate_source.SOURCE_S2,
    s6_candidate_source.SOURCE_S6,
})


def is_strategy_source(source) -> bool:
    """True for a live strategy's source, False for the legacy watchlist.

    Fails closed in the direction that keeps the STRICTER path: an
    unrecognised source is not a strategy source, so it gets the legacy
    behaviour it was presumably written against rather than gates it has
    never been tested with.
    """
    return getattr(source, "name", None) in STRATEGY_SOURCES


def _s6_candidate_row(source, symbol):
    """The published row this order was built from, or None.

    Best-effort on purpose: the ORB measurements it carries make the
    stored position describe the range it actually broke out of, but a
    source that cannot produce the row must not be able to fail an order
    that has already reached the broker. A missing row leaves those
    columns NULL, which `exit_policy` reads as "not measured" -- unlike a
    fabricated range, which would silently move the structural stop.
    """
    getter = getattr(source, "candidate_row", None)
    if not callable(getter):
        return None
    try:
        return getter(symbol)
    except Exception:  # noqa: BLE001
        logger.warning("S6 candidate row for %s unreadable; the position will "
                       "carry no range measurements", symbol, exc_info=True)
        return None


#: Another process held the order idempotency lock. A collision, not a
#: fault in the candidate.
IDEMPOTENCY_LOCK_BUSY = "IDEMPOTENCY_LOCK_BUSY"


def _session_permitted(source, rollout) -> bool:
    """May THIS strategy order in the session we are actually in?

    Per strategy, because `rollout.regular_session_only` is one global
    flag consumed by a buy cycle three strategies share. Turning it off
    to let S6 trade the daytime session would have removed the
    regular-hours restriction from S1's live orders at the same time --
    S1 is LIMITED_LIVE with a real open position, and nothing about
    enabling an S6 session is a reason to widen S1's trading hours.

    So S6 answers from its OWN session policy, which lists only sessions
    whose KIS order route the specification defines, and every other
    strategy keeps the global flag exactly as before.

    Every name used here is bound at module import. The first version
    imported `scanners.base.scan_session` and `s6_live.candidate_source`
    lazily, inside the cycle, and that cost seven order-intent-ledger
    tests: importing mid-cycle re-initialised module state the tests had
    already patched, so a ledger that should have refused a duplicate
    accepted it. A hot path in the order cycle is the worst possible
    place to import anything.
    """
    if getattr(source, "name", None) == s6_candidate_source.SOURCE_S6:
        # Asked of the shared resolver rather than of the session policy
        # alone. `s6_sessions.orders_allowed` answers "has the rollout
        # reached this session", which is necessary but not sufficient:
        # it knows nothing about whether KIS defines a route, and nothing
        # about the calendar. `scan_session.session_at` is deliberately
        # calendar-independent (a holiday still has a premarket window on
        # the clock), so on its own it would have called a Saturday
        # evening OVERNIGHT_DAYTIME and permitted an order into a day the
        # market never opened.
        return session_capability.order_session(
            strategy_id=s6_sessions.STRATEGY_ID) is not None

    return pso.get_us_market_session() == "regular" \
        if rollout.regular_session_only else True


#: Who this cycle identifies itself as while holding the lock.
_EXEC_LOCK_OWNER_ENTRY = "S6_ENTRY_SUBMIT"

#: Reason codes for an entry dropped by the under-lock revalidation.
#:
#: Every one of these means the prepared entry was CORRECT when it was
#: prepared and is not correct now. Dropping is the whole point: the
#: analysis that produced it ran without the execution lock, so between
#: deciding and submitting, the world was free to move.
REVALIDATION_HALT = "REVALIDATION_HALT"
REVALIDATION_ENTRY_OFF = "REVALIDATION_ENTRY_OFF"
REVALIDATION_ENTRY_DISABLED_ENV = "REVALIDATION_ENTRY_DISABLED_ENV"
REVALIDATION_EXIT_IN_FLIGHT = "REVALIDATION_EXIT_IN_FLIGHT"
REVALIDATION_SYMBOL_HELD = "REVALIDATION_SYMBOL_HELD"
REVALIDATION_STATE_UNREADABLE = "REVALIDATION_STATE_UNREADABLE"


def _revalidate_before_submit(*, symbol, broker, conn, instrument, order_intent,
                              buffered_price, live_state, signal=None,
                              now=None):
    """Re-ask, holding the execution lock, everything that could have
    changed while the analysis ran unlocked.

    Returns `(reason_code, detail)` to DROP the prepared entry, or None
    to proceed. On success it REFRESHES `live_state` so the gate context
    is built from what is true now rather than from what was true when
    the candidate was picked.

    Everything here is either a local read or a broker read that the
    submission is about to depend on anyway. What it does NOT re-do is
    the gate: the entry-limit state and the reconciliation snapshot are
    gathered by the context builder, which runs inside this same lock,
    so the caps, SYMBOL_ALREADY_HELD and reconciliation are already
    evaluated against current state and are not duplicated here.

    Naming the collector in prose here is deliberately avoided:
    tests/test_s1_live_isolation.py counts its call sites in this file
    to prove there is ONE pipeline rather than two, and a mention in a
    docstring reads to that check exactly like a second call.

    Fails CLOSED. A read that cannot be completed drops the entry --
    an entry is an opportunity, and the cost of missing one is a tick.
    """
    from live_pilot import posture as live_posture

    # Signal freshness is NOT re-asked here, and that is deliberate.
    #
    # It was, briefly, and it stopped S6 trading. `SIGNAL_VALID_SECONDS`
    # is 120 while an entry cycle legitimately takes two to five minutes,
    # so measuring the signal against the real clock at submit time
    # expired every candidate -- 2 of 2 on 2026-09-02 -- and replaced
    # lock starvation with expiry starvation.
    #
    # The gate asks this against the cycle's own clock, which makes the
    # 120s window effectively "this cycle". Whether that window SHOULD
    # be measured across the pipeline is a real question about a risk
    # parameter, and the answer is not this function's to give: changing
    # when a signal counts as stale changes which trades happen, and
    # that belongs to the strategy, not to a lock refactor.
    #
    # What this function may do is make the gate's inputs current. What
    # it may not do is quietly redefine one of them.

    # 1. The two kill switches and the operator posture. All three were
    #    checked once at the top of the cycle, minutes ago.
    try:
        if ops_kill_switch.is_halted():
            return (REVALIDATION_HALT,
                    "operations HALT was set while this entry was being prepared")
        if not ops_kill_switch.is_entry_allowed():
            return (REVALIDATION_ENTRY_OFF,
                    "ENTRY_OFF was set while this entry was being prepared")
    except Exception as exc:  # noqa: BLE001 -- unreadable switch = no order
        return (REVALIDATION_STATE_UNREADABLE,
                f"the kill switch could not be re-read: {type(exc).__name__}")

    if live_posture.env_bool(os.environ, live_posture.FLAG_ENTRY_DISABLED, False):
        return (REVALIDATION_ENTRY_DISABLED_ENV,
                f"{live_posture.FLAG_ENTRY_DISABLED} was set while this entry "
                "was being prepared")

    # 2. Re-read this symbol's position while holding the mutation lock.
    #    An exit for THIS symbol blocks its re-entry, but an unrelated
    #    position's exit does not suppress the rest of a multi-position
    #    strategy. Also catches the symbol becoming held by anyone,
    #    including by an earlier candidate in this very cycle.
    try:
        from s6_live import position_store as _s6_store

        existing = _s6_store.load_by_symbol(conn, symbol)
        if existing is not None and existing.get("status") in _s6_store.LIVE_STATUSES:
            if existing.get("exit_submitted"):
                return (REVALIDATION_EXIT_IN_FLIGHT,
                        f"an S6 exit for {symbol} reached the broker while this "
                        "entry was being prepared")
            return (REVALIDATION_SYMBOL_HELD,
                    f"{symbol} became live in the canonical store "
                    f"({existing.get('status')}) while this entry was being prepared")
    except Exception as exc:  # noqa: BLE001 -- fail closed
        return (REVALIDATION_STATE_UNREADABLE,
                f"the S6 position store could not be re-read: {type(exc).__name__}")

    # 3. A resting order for this symbol. The pre-lock read of the open
    #    order book is now minutes old, and a duplicate BUY is the one
    #    mistake this whole lock exists to make impossible.
    try:
        open_orders = broker.get_open_orders() or []
    except Exception as exc:  # noqa: BLE001 -- fail closed
        return (REVALIDATION_STATE_UNREADABLE,
                f"the open order book could not be re-read: {type(exc).__name__}")
    # REFRESHED, not decided here. The gate already refuses a symbol
    # with a resting order, with its own reason code and its own audit
    # event, and the execution engine's idempotency ledger refuses a
    # repeated signal behind it. Both were reaching the RIGHT answer from
    # a STALE reading -- the open-order book as it looked before the
    # analysis. Handing them a current one is the fix; making the
    # decision here as well would only shadow the more specific refusal
    # and lose DUPLICATE_BLOCKED from the audit trail.
    live_state["has_open_order_for_symbol"] = any(
        (o.get("pdno") or o.get("PDNO")) == symbol
        for o in open_orders if hasattr(o, "get")
    )

    # 4. Orderable cash. Another entry in this cycle may have spent it,
    #    and an order the account cannot fund is a rejection at best.
    try:
        available_usd = broker.get_orderable_usd(instrument, buffered_price)
    except Exception as exc:  # noqa: BLE001 -- fail closed
        return (REVALIDATION_STATE_UNREADABLE,
                f"orderable cash could not be re-read: {type(exc).__name__}")
    # Rounded to cents before comparing. 7 x 17.01 is 119.07000000000001
    # in binary floating point, so an account holding exactly 119.07 --
    # which can afford the order -- would be refused for being a
    # hundred-billionth of a cent short.
    # Refreshed, not re-decided -- same reasoning as the open-order book
    # above. The gate already refuses an order the account cannot fund,
    # with its own CASH code; it was simply doing so against the balance
    # as it stood before the analysis. A second cash rule here would be a
    # new safety gate with subtly different arithmetic, which is worse
    # than the staleness it would be trying to fix.
    live_state["available_usd"] = available_usd
    return None


def run_live_buy_entry_cycle(*, broker, live_rollout=None, now=None,
                             candidate_source=None):
    """Returns a results dict: {"submitted": [...], "blocked": [(symbol, reason)], "skipped": [...]}.
    Never raises for a per-symbol failure -- only for a structural
    precondition failure that makes the WHOLE cycle unsafe to run at all
    (config invalid, halted, commit mismatch).

    `candidate_source` supplies the two things this cycle used to answer
    inline: which symbols to evaluate, and which symbols the Order Gate
    is told are allowed. Omitting it resolves the source from the
    environment, which yields the legacy watchlist source unless
    `S1_LIVE_SOURCE_ENABLED` is explicitly set -- so the default path is
    the one that shipped, symbol for symbol.

    Only the SOURCE is pluggable. Every gate below -- allow-list check,
    price re-validation, orderable cash, duplicate order, entry limits,
    kill switch, reconciliation, the Execution Engine -- is shared by
    every source and exists exactly once. A second candidate source must
    never mean a second pipeline: two pipelines are two ideas of what is
    safe, and they diverge silently.
    """
    current = now or datetime.now(timezone.utc)
    results = {"submitted": [], "blocked": [], "skipped": []}
    cycle_run_id = shadow_audit.new_run_id()

    rollout = live_rollout or LiveRolloutConfig.from_env()
    try:
        rollout.validate()
    except LiveRolloutConfigError as exc:
        reason = f"live_rollout config invalid, refusing to run: {exc}"
        _persist_blocked_record(
            symbol=_CYCLE_LEVEL_SYMBOL, risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
        )
        _audit_cycle_block(cycle_run_id, shadow_audit.CONFIG_BLOCKED, "CONFIG_INVALID", reason, now=current)
        raise KISLiveTradingError(reason) from exc

    if not rollout.enabled:
        reason = "live_rollout.enabled is False -- KIS live entries are not active"
        _persist_blocked_record(
            symbol=_CYCLE_LEVEL_SYMBOL, risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
        )
        _audit_cycle_block(cycle_run_id, shadow_audit.CONFIG_BLOCKED, "ROLLOUT_DISABLED", reason, now=current)
        raise KISLiveTradingError(reason)

    if ops_kill_switch.is_halted():
        reason = "operations HALT is set -- no automatic order attempts permitted"
        _persist_blocked_record(
            symbol=_CYCLE_LEVEL_SYMBOL, risk_gate_result="HALT", rejection_reason=reason, now=current,
        )
        _audit_cycle_block(cycle_run_id, shadow_audit.HALT_BLOCKED, "HALT", reason, now=current)
        raise KISLiveTradingError(reason)
    if not ops_kill_switch.is_entry_allowed():
        reason = "ENTRY_OFF (kill_switch_state) is set -- new entries blocked"
        _persist_blocked_record(
            symbol=_CYCLE_LEVEL_SYMBOL, risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
        )
        _audit_cycle_block(cycle_run_id, shadow_audit.HALT_BLOCKED, "ENTRY_OFF", reason, now=current)
        raise KISLiveTradingError(reason)

    # Two INDEPENDENT entry permissions, both fail-closed, deliberately not
    # merged into one:
    #
    #   kill_switch_state == ENTRY_DISABLED  (checked immediately above)
    #       the runtime emergency stop. Persisted, so it survives a restart
    #       and takes effect without touching a file the operator may not
    #       be able to reach mid-incident.
    #
    #   ENTRY_DISABLED in the environment      (checked here)
    #       the deployment/operator posture. It is what `resolve_posture()`
    #       reads, and it is what an operator editing shared env expects to
    #       be obeyed.
    #
    # Before this gate existed the second one was read ONLY by
    # live_pilot/posture.py, which `live_pilot/armed.py:entry_cycle()` does
    # not call -- so setting ENTRY_DISABLED=true in shared env produced an
    # OBSERVE posture in every report while this cycle went on placing
    # orders. An operator following the documented incident procedure would
    # have believed new entries were stopped when they were not.
    #
    # Neither switch is synchronised into the other: they answer different
    # questions and either one alone must be able to stop new entries.
    # Neither applies to the SELL path -- an entry block that also blocked
    # liquidation would trap the account in the position the block exists
    # to escape.
    # Imported here, not at module scope: live_pilot/armed.py imports THIS
    # module, so a top-level import would close the cycle.
    from live_pilot import posture as live_posture

    # Scoped deliberately to ENTRY_DISABLED alone rather than to the full
    # ARMED posture. "Are live entries active at all" is already answered
    # above by `rollout.enabled`, which callers inject -- re-deriving it
    # from os.environ here would mean a caller could hold an enabled
    # rollout config and still be refused because the process environment
    # disagreed with it. The gap this closes is narrower and specific:
    # ENTRY_DISABLED was read ONLY by live_pilot/posture.py.
    if live_posture.env_bool(os.environ, live_posture.FLAG_ENTRY_DISABLED, False):
        reason = (
            f"{live_posture.FLAG_ENTRY_DISABLED} is set in the environment "
            "-- new entries blocked by operator posture"
        )
        _persist_blocked_record(
            symbol=_CYCLE_LEVEL_SYMBOL, risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
        )
        _audit_cycle_block(cycle_run_id, shadow_audit.CONFIG_BLOCKED,
                           "ENTRY_DISABLED_ENV", reason, now=current)
        raise KISLiveTradingError(reason)

    validated_commit = _get_validated_commit()
    deployed_commit = _get_deployed_commit()
    if not validated_commit or validated_commit != deployed_commit:
        reason = (
            f"validated commit {validated_commit!r} does not match deployed commit "
            f"{deployed_commit!r} -- refusing to run an unvalidated deployment"
        )
        _persist_blocked_record(
            symbol=_CYCLE_LEVEL_SYMBOL, risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
        )
        _audit_cycle_block(cycle_run_id, shadow_audit.CONFIG_BLOCKED, "COMMIT_MISMATCH", reason, now=current)
        raise KISLiveTradingError(reason)

    allowed_account_no = _get_allowed_account_no()
    if not allowed_account_no:
        reason = "KIS_ALLOWED_ACCOUNT_NO is not configured -- refusing to run"
        _persist_blocked_record(
            symbol=_CYCLE_LEVEL_SYMBOL, risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
        )
        _audit_cycle_block(cycle_run_id, shadow_audit.CONFIG_BLOCKED, "ACCOUNT_UNCONFIGURED", reason, now=current)
        raise KISLiveTradingError(reason)

    is_regular_session = _session_permitted(candidate_source, rollout)

    # Resolved AFTER every structural precondition above, so a cycle that
    # was going to refuse anyway never reads a candidate file.
    # `watchlist_module=pso` hands over THIS module's own reference. It
    # must not be re-imported inside the source: test_ai_analysis.py
    # pops "paper_strategy_order" from sys.modules and leaves it popped,
    # so a fresh import would build a different module object than the
    # one `klt.pso` -- and therefore every existing monkeypatch -- uses.
    source = candidate_source or s1_candidate_source.resolve(
        rollout, trading_day=us_trading_day(current), watchlist_module=pso)
    # One evaluation of the allow-list per cycle. Re-reading it per symbol
    # would let the set change underneath a cycle that had already made
    # decisions against the earlier value.
    allowed_symbols = source.allowed_symbols()
    logger.info("candidate source: %s", source.describe())

    watchlist = source.symbols()
    kis_validation = KISValidationProvider(broker, instrument_lookup=lambda s: _build_instrument(s, allowed_symbols))

    conn = state_db.open_db()
    try:
        for symbol in watchlist:
            # CODEX-048: one shadow_run_id per symbol evaluation ties every
            # step of that evaluation together, and the finally-block below
            # guarantees exactly one terminal event per run -- there is no
            # path (block, approve, or unexpected exception) that leaves a
            # run without a recorded outcome.
            run_id = shadow_audit.new_run_id()
            outcome = {"result": shadow_audit.RESULT_BLOCKED, "reason_code": None,
                       "detail": None, "internal_order_id": None}
            terminal_recorded = False
            try:
                if symbol not in allowed_symbols:
                    results["skipped"].append((symbol, "not in live_rollout.allowed_symbols"))
                    _persist_blocked_record(
                        symbol=symbol, risk_gate_result="BLOCKED",
                        rejection_reason="symbol not in live_rollout.allowed_symbols", now=current,
                    )
                    outcome["reason_code"] = "SYMBOL_NOT_ALLOWED"
                    _audit(run_id, shadow_audit.INSTRUMENT_BLOCKED, shadow_audit.RESULT_BLOCKED,
                           symbol=symbol, reason_code="SYMBOL_NOT_ALLOWED", now=current)
                    continue

                # Security type, re-checked here even though the publisher
                # already filtered on it. The candidate file is an artifact
                # on disk: it can be stale, hand-edited, or left over from a
                # run against a different master. This is the last point
                # before an order where "is this actually a common stock"
                # can still be asked, and the answer comes from KIS's own
                # master rather than a name rule.
                #
                # ETP is refused whole. Distinguishing an ETF from a
                # leveraged or inverse one is unnecessary when none may be
                # bought, and attempting it would only create a way to get
                # the answer wrong. UNKNOWN is refused too: a symbol the
                # master does not list is not a symbol we know is safe.
                # Scoped to the S1 source. In the live configuration that is
                # the only source there is (`S1_LIVE_SOURCE_ENABLED=true`),
                # so the gate always applies to anything that can actually
                # trade. The legacy watchlist source keeps the mechanism it
                # shipped with -- operator-curated `allowed_symbols` -- rather
                # than acquiring a new refusal it was never written against.
                try:
                    classification = (
                        s1_security_type.require_live_eligible(symbol)
                        if is_strategy_source(source) else None)
                except s1_security_type.SecurityTypeUnavailable as exc:
                    reason = str(exc)
                    results["skipped"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, risk_gate_result="BLOCKED",
                        rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = reason.split(":")[0]
                    _audit(run_id, shadow_audit.INSTRUMENT_BLOCKED, shadow_audit.RESULT_BLOCKED,
                           symbol=symbol, reason_code=outcome["reason_code"],
                           detail=reason, now=current)
                    continue
                if classification is not None:
                    # No strategy in the message. This path is shared and
                    # the line said "S1" for every strategy that used it,
                    # so an S6 entry's security check read as S1 to anyone
                    # reading the log for evidence of what a strategy did.
                    # The strategy is not yet known here either -- it
                    # arrives with qualification below -- and naming the
                    # wrong one is worse than naming none.
                    logger.info("entry candidate %s verified as %s on %s "
                                "(KIS master %s)", symbol,
                                classification.security_type,
                                classification.exchange, classification.asof)

                # PHASE 4A: qualification is the ONLY source-specific step.
                # The legacy source still applies analyze_stock +
                # SCORE_THRESHOLD, byte for byte. The S1 source uses its
                # own validated candidate row instead, because requiring
                # an S1 candidate to also clear an unrelated older scoring
                # model would mean the thing that actually trades is "S1
                # AND legacy score" -- not the strategy month 1 measured.
                # Everything below this point is shared by both.
                qualified = source.qualify(
                    symbol, analyze=pso.analyze_stock, score_threshold=SCORE_THRESHOLD)
                if not qualified.qualified:
                    results["skipped"].append((symbol, qualified.detail or "not qualified"))
                    outcome["result"] = shadow_audit.RESULT_INFO
                    outcome["reason_code"] = qualified.reason_code or "NOT_QUALIFIED"
                    continue
                analysis = {"price": qualified.price, "score": qualified.score}

                _audit(run_id, shadow_audit.SIGNAL_RECEIVED, shadow_audit.RESULT_INFO, symbol=symbol,
                       reason_code="SCORE_THRESHOLD_MET", now=current)

                try:
                    instrument = _build_instrument(symbol, allowed_symbols)
                    signal = build_signal(
                        # The strategy identity comes from whoever
                        # qualified the candidate. A trade recorded under
                        # the legacy id when an S1 candidate produced it
                        # would be untraceable back to the scanner and to
                        # the month of discovery data behind it. The
                        # legacy source still yields the legacy id.
                        strategy_id=qualified.strategy_id, strategy_version="v1",
                        config_version="live_rollout_v1", code_commit=deployed_commit,
                        symbol=symbol, exchange=instrument.exchange, signal_price=analysis["price"],
                        score=analysis["score"], entry_reason=qualified.entry_reason,
                        valid_for_seconds=SIGNAL_VALID_SECONDS, now=current,
                        # The SCANNER's signal id, not a fresh one per cycle.
                        #
                        # `execution/idempotency.py` already refuses a
                        # second attempt on (signal_id, symbol, side,
                        # trading_date) -- a durable UNIQUE constraint that
                        # survives a restart. Minting `sig-<uuid4>` here
                        # every cycle handed that guard a different key each
                        # time, so it could never fire: one scanner
                        # candidate could produce an unbounded series of
                        # real BUY attempts.
                        #
                        # SLGN on 2026-09-03 is what that looks like. The
                        # same published row bought twice, three minutes
                        # apart, as sig-dd73fb3399f84167 and then
                        # sig-e37f9c095891414f; the first was cancelled
                        # zero-filled, the second filled 3 @ 41.61 and was
                        # lost. A genuinely new scanner generation carries a
                        # new source_signal_id and is still free to enter.
                        #
                        # Falls back to a generated id when a source does
                        # not publish one, which is exactly today's
                        # behaviour for those sources -- this narrows the
                        # ones that DO, and widens nothing.
                        signal_id=getattr(qualified, "source_signal_id", None) or None,
                    )
                except (InstrumentError, SignalError) as exc:
                    reason = f"signal/instrument construction failed: {exc}"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=analysis["price"], risk_gate_result="BLOCKED",
                        rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = "INSTRUMENT_INVALID"
                    _audit(run_id, shadow_audit.INSTRUMENT_BLOCKED, shadow_audit.RESULT_BLOCKED,
                           symbol=symbol, reason_code="INSTRUMENT_INVALID", detail=reason, now=current)
                    continue

                try:
                    kis_quote = kis_validation.get_price_quote(symbol)
                except MarketDataProviderError as exc:
                    reason = f"KIS price re-check failed: {exc}"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=signal.signal_price, risk_gate_result="BLOCKED",
                        rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = "PRICE_UNAVAILABLE"
                    _audit(run_id, shadow_audit.PRICE_DEVIATION_BLOCKED, shadow_audit.RESULT_BLOCKED,
                           symbol=symbol, signal_id=signal.signal_id,
                           reason_code="PRICE_UNAVAILABLE", detail=reason, now=current)
                    continue

                try:
                    account_snapshot = broker.get_account_snapshot()
                except KISBrokerError as exc:
                    reason = f"KIS account read failed: {exc}"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=signal.signal_price, kis_price=kis_quote.price_usd,
                        risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = "ACCOUNT_READ_FAILED"
                    _audit(run_id, shadow_audit.CASH_BLOCKED, shadow_audit.RESULT_BLOCKED, symbol=symbol,
                           signal_id=signal.signal_id, reason_code="ACCOUNT_READ_FAILED",
                           detail=reason, now=current)
                    continue

                # ORACLE-CASH-01: the same per-candidate orderable-amount
                # read the Shadow path uses. The account snapshot carries
                # no cash figure (TTTS3012R does not return one), and KIS
                # answers orderable cash per (symbol, exchange, limit
                # price) -- so it is asked at `buffered_price`, the exact
                # price the OrderIntent below is built with.
                buffered_price = kis_quote.price_usd
                try:
                    # One read per candidate; reused for sizing, the gate
                    # context and the shadow record below.
                    available_usd = broker.get_orderable_usd(instrument, buffered_price)
                except KISOrderableCashUnavailableError as exc:
                    reason = f"KIS orderable-amount read unusable: {exc.diagnostic()}"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=signal.signal_price, kis_price=kis_quote.price_usd,
                        risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = ORDERABLE_CASH_UNAVAILABLE
                    _audit(run_id, shadow_audit.CASH_BLOCKED, shadow_audit.RESULT_BLOCKED, symbol=symbol,
                           signal_id=signal.signal_id, reason_code=ORDERABLE_CASH_UNAVAILABLE,
                           detail=reason, now=current)
                    continue

                # Variable sizing. `whole_shares_affordable` divides the
                # broker's own orderable amount by the price we would
                # actually pay, so the answer is per candidate rather
                # than a single account-level verdict: $20.96 buys one
                # share of a $10.91 name and none of a $40.19 one, and
                # only the second is a reason to skip.
                #
                # The per-order cap is applied only when an operator set
                # one. It defaulted to 1 for LIMITED_LIVE and stayed
                # there, which silently made every order a single share
                # however much cash was available.
                balance_qty = whole_shares_affordable(available_usd, buffered_price)
                quantity = (min(balance_qty, rollout.max_quantity_per_order)
                            if rollout.max_quantity_per_order is not None
                            else balance_qty)
                # Logged for every candidate, including the ones sized to
                # zero. A skipped candidate and an unaffordable one look
                # identical downstream, and the account-level cash figure
                # cannot tell them apart -- the same $20.96 is a BUY for
                # one name and a skip for the next.
                logger.info(
                    "SIZING %s live_price=%.4f buffered_price=%.4f "
                    "orderable_usd=%.2f max_orderable_qty=%d cap=%s target_qty=%d",
                    symbol, kis_quote.price_usd, buffered_price, available_usd, balance_qty,
                    rollout.max_quantity_per_order
                    if rollout.max_quantity_per_order is not None else "none",
                    quantity)
                if quantity < 1:
                    reason = "insufficient KIS orderable cash for even 1 share"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=signal.signal_price, kis_price=kis_quote.price_usd,
                        account_available_usd=available_usd, risk_gate_result="BLOCKED",
                        rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = INSUFFICIENT_CASH
                    _audit(run_id, shadow_audit.CASH_BLOCKED, shadow_audit.RESULT_BLOCKED, symbol=symbol,
                           signal_id=signal.signal_id, reason_code=INSUFFICIENT_CASH,
                           detail=reason, now=current)
                    continue

                # The general live BUY path carries its session for the
                # same reason the bootstrap and the exit path do: without
                # it the broker falls back to `_session_hint` ("REGULAR")
                # and a daytime entry is addressed to an endpoint that is
                # not open at that hour. None is a refusal here, not a
                # fallback -- the entry is skipped rather than guessed.
                # Bound at module import, never here: importing mid-cycle
                # re-initialises module state a caller may have patched,
                # which this file has been bitten by before.
                # ROUTING only -- deliberately without a strategy.
                #
                # "Which endpoint does this session use" and "is this
                # strategy allowed to open a position" are different
                # questions, and folding the second into the first here
                # blocked every strategy that is stood down or simply not
                # in the registry, at the point where the code was only
                # trying to address an envelope. Entry permission is the
                # gate's job (`entry_disabled`), applied once, where the
                # decision is already made and audited.
                entry_route_session = session_capability.route_session(now=current)
                if entry_route_session is None:
                    cap = session_capability.current_capability(now=current)
                    reason = ("no KIS order route is available for the current "
                              f"session: {cap.entry_reason}")
                    results["blocked"].append((symbol, reason))
                    outcome["reason_code"] = "NO_ENTRY_ROUTE_FOR_SESSION"
                    _audit(run_id, shadow_audit.INSTRUMENT_BLOCKED,
                           shadow_audit.RESULT_BLOCKED, symbol=symbol,
                           signal_id=signal.signal_id,
                           reason_code="NO_ENTRY_ROUTE_FOR_SESSION",
                           detail=reason, now=current)
                    continue

                try:
                    order_intent = OrderIntent(
                        internal_order_id=f"kislive-{symbol}-{uuid.uuid4().hex[:12]}",
                        signal_id=signal.signal_id, strategy_id=signal.strategy_id, symbol=symbol,
                        exchange=instrument.exchange, side="buy", quantity=quantity, order_type="limit",
                        limit_price=buffered_price, stop_price=None, target_price=None, created_at=current,
                        session=entry_route_session,
                    )
                except OrderIntentError as exc:
                    reason = f"order intent construction failed: {exc}"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=signal.signal_price, kis_price=buffered_price,
                        account_available_usd=available_usd, risk_gate_result="BLOCKED",
                        rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = "ORDER_INTENT_INVALID"
                    _audit(run_id, shadow_audit.INSTRUMENT_BLOCKED, shadow_audit.RESULT_BLOCKED,
                           symbol=symbol, signal_id=signal.signal_id,
                           reason_code="ORDER_INTENT_INVALID", detail=reason, now=current)
                    continue

                try:
                    open_orders = broker.get_open_orders()
                except KISBrokerError as exc:
                    reason = f"KIS open-orders read failed: {exc}"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=signal.signal_price, kis_price=buffered_price,
                        planned_quantity=order_intent.quantity, planned_limit_price=order_intent.limit_price,
                        account_available_usd=available_usd, risk_gate_result="BLOCKED",
                        rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = "OPEN_ORDER_READ_FAILED"
                    _audit(run_id, shadow_audit.RECONCILIATION_BLOCKED, shadow_audit.RESULT_BLOCKED,
                           symbol=symbol, signal_id=signal.signal_id,
                           internal_order_id=order_intent.internal_order_id,
                           reason_code="OPEN_ORDER_READ_FAILED", detail=reason, now=current)
                    continue
                has_open_order_for_symbol = any(
                    (o.get("pdno") or o.get("PDNO")) == symbol for o in open_orders
                )

                try:
                    existing_positions = broker.get_positions()
                except KISBrokerError:
                    existing_positions = []
                existing_position_qty = next(
                    (p.quantity for p in existing_positions if p.symbol == symbol), 0
                )
                planned_stop_price = buffered_price * (1 + risk_config.STOP_LOSS_RATE)
                planned_risk_per_share = buffered_price - planned_stop_price
                planned_target_price = buffered_price + planned_risk_per_share * strat_cfg.TARGET_1_R_MULTIPLE
                price_diff_percent = compute_price_deviation_percent(signal.signal_price, kis_quote.price_usd)

                # S1 only. The previous-close deviation check is the right
                # question for a seconds-old scalping signal and the wrong
                # one for S1, whose signal price IS yesterday's close --
                # on 2026-08-18 it refused all nine ranked candidates, the
                # tightest by 0.46%. The verdict below asks instead whether
                # the price sits inside the instrument's own trading-day
                # range, which a stale or wrong-exchange quote still fails.
                #
                # Left as None for the legacy source, which keeps the 0.30%
                # check exactly as it was.
                execution_price_verdict = None
                if is_strategy_source(source):
                    execution_price_verdict = s1_execution_price.evaluate_symbol(
                        symbol, broker=broker, instrument=instrument)
                    if not execution_price_verdict.passed:
                        reason = (f"execution-price check failed: "
                                  f"{execution_price_verdict.reason_code} "
                                  f"({execution_price_verdict.detail})")
                        results["blocked"].append((symbol, reason))
                        _persist_blocked_record(
                            symbol=symbol, risk_gate_result="BLOCKED",
                            rejection_reason=reason, signal_price=signal.signal_price,
                            kis_price=kis_quote.price_usd, now=current,
                        )
                        outcome["reason_code"] = execution_price_verdict.reason_code
                        _audit(run_id, shadow_audit.INSTRUMENT_BLOCKED,
                               shadow_audit.RESULT_BLOCKED, symbol=symbol,
                               reason_code=execution_price_verdict.reason_code,
                               detail=execution_price_verdict.detail, now=current)
                        continue
                    logger.info("S1 execution price OK for %s: %s",
                                symbol, execution_price_verdict.detail)

                # The two values the revalidation refreshes once the
                # execution lock is held. Read through this dict rather
                # than captured as defaults: the builder runs INSIDE the
                # lock, and binding them here would hand the gate the
                # numbers from before the analysis, which is exactly the
                # staleness moving the lock was meant to remove.
                live_state = {
                    "available_usd": available_usd,
                    "has_open_order_for_symbol": has_open_order_for_symbol,
                }

                def _buy_ctx_builder(
                    reconciliation,
                    signal=signal, instrument=instrument, order_intent=order_intent,
                    kis_price=kis_quote.price_usd, live_state=live_state,
                    execution_price_verdict=execution_price_verdict,
                ):
                    # Collected inside the builder, so it is read at gate
                    # time rather than at candidate-selection time -- the
                    # execution engine has already registered this
                    # attempt's idempotency row by now, which is why the
                    # attempt excludes itself from its own counts.
                    limits = entry_limits.collect(
                        broker=broker, conn=conn, rollout=rollout, now=current,
                        exclude_internal_order_id=order_intent.internal_order_id,
                        # The ENGINE's reading, not this module's. The
                        # snapshot handed to this builder was built by
                        # `submit_buy_order` moments ago inside the same
                        # lock, so the position book is read once per
                        # submission instead of twice. Passing None when
                        # a snapshot does not carry it leaves the
                        # original broker read in place, which is what a
                        # hand-built snapshot in a test still gets.
                        kis_position_quantities=getattr(
                            reconciliation, "kis_position_quantities", None),
                    )
                    # A strategy stood down for new entries must be
                    # refused HERE, at the gate, by the same policy the
                    # readiness checker and the capability resolver read.
                    #
                    # This was hardcoded False while `strategy_entry_policy`
                    # existed and was consulted only by the report and the
                    # resolver -- so the stand-down was visible everywhere
                    # except the one place that can stop an order. Route
                    # resolution deliberately does NOT apply the policy
                    # (addressing an envelope is not permission), which
                    # makes wiring it here the other half of that split
                    # rather than a duplicate of it.
                    return order_gate.BuyGateContext(
                        execution_broker="kis", live_order_enabled=True,
                        entry_disabled=not strategy_entry_policy.entry_enabled(
                            order_intent.strategy_id),
                        validated_commit=validated_commit, deployed_commit=deployed_commit,
                        kis_account_no=account_snapshot.account_id, allowed_account_no=allowed_account_no,
                        order_intent=order_intent, instrument=instrument, signal=signal,
                        is_regular_session=is_regular_session, kis_price_usd=kis_price,
                        max_price_deviation_percent=rollout.max_price_deviation_percent,
                        usd_orderable_cash=live_state["available_usd"],
                        has_open_order_for_symbol=live_state["has_open_order_for_symbol"],
                        has_order_for_signal_id=False, allowed_symbols=allowed_symbols,
                        # CODEX-044: supplied BY the Execution Engine from its
                        # own live KIS reads -- this pipeline cannot assert
                        # reconciliation status, only pass through what the
                        # engine actually observed.
                        reconciliation=reconciliation,
                        entry_limits=limits,
                        now=current,
                        execution_price_check=execution_price_verdict,
                    )

                def _shadow_record(risk_gate_result, rejection_reason=None):
                    return shadow_mode.build_record(
                        signal_id=signal.signal_id, strategy_id=signal.strategy_id,
                        strategy_version="v1", code_commit=deployed_commit, symbol=symbol, side="buy",
                        alpaca_signal_price=signal.signal_price, kis_validation_price=kis_quote.price_usd,
                        price_difference_percent=price_diff_percent, planned_quantity=order_intent.quantity,
                        planned_limit_price=order_intent.limit_price, stop_price=planned_stop_price,
                        target_price=planned_target_price, risk_gate_result=risk_gate_result,
                        rejection_reason=rejection_reason, account_available_usd=available_usd,
                        existing_position_quantity=existing_position_qty,
                        existing_open_order=has_open_order_for_symbol, now=current,
                    )

                # Stamped HERE rather than reused from `current`.
                # `current` is the cycle's start -- the same value every
                # symbol in the loop shares -- which is exactly how the
                # order events ended up with four transitions carrying
                # one timestamp and no measurable latency between them.
                submit_at = datetime.now(timezone.utc)

                try:
                    # THE CRITICAL SECTION, and only this.
                    #
                    # Everything above -- candidates, precision watch,
                    # pre-trade validation, quotes, sizing -- ran without
                    # this lock. That is the point: holding it across the
                    # whole cycle is what starved the one-minute exit
                    # monitor down to 1 acquisition in 29 on 2026-09-02,
                    # and enforced a 180-second TTL at 782 and 836
                    # seconds. What is serialised here is the mutation,
                    # not the thinking that led to it.
                    #
                    # The gate context builder runs INSIDE this block, so
                    # `entry_limits` (the caps and SYMBOL_ALREADY_HELD)
                    # and the reconciliation snapshot are gathered against
                    # current state rather than re-checked afterwards.
                    with execution_lock.hold(_EXEC_LOCK_OWNER_ENTRY):
                        dropped = _revalidate_before_submit(
                            symbol=symbol, broker=broker, conn=conn,
                            instrument=instrument, order_intent=order_intent,
                            buffered_price=buffered_price, live_state=live_state,
                            signal=signal, now=current)
                        if dropped is not None:
                            revalidation_code, revalidation_detail = dropped
                            logger.info(
                                "ENTRY_REVALIDATION_DROPPED %s reason=%s: %s",
                                symbol, revalidation_code, revalidation_detail)
                            results["blocked"].append((symbol, revalidation_detail))
                            _persist_blocked_record(
                                symbol=symbol,
                                account_available_usd=live_state["available_usd"],
                                risk_gate_result="BLOCKED",
                                rejection_reason=revalidation_detail, now=current,
                            )
                            outcome["result"] = shadow_audit.RESULT_BLOCKED
                            outcome["reason_code"] = revalidation_code
                            outcome["detail"] = revalidation_detail
                            _audit(run_id, shadow_audit.GATE_REJECTED,
                                   shadow_audit.RESULT_BLOCKED, symbol=symbol,
                                   signal_id=signal.signal_id,
                                   internal_order_id=order_intent.internal_order_id,
                                   reason_code=revalidation_code,
                                   detail=revalidation_detail, now=current)
                            continue
                        # CODEX-048: audit_run_id lets the Execution Engine
                        # record GATE_APPROVED and EXECUTION_PLANNED BEFORE it
                        # calls the broker. Recording them here, after this
                        # call returns, would leave a crash during the broker
                        # call with no audit of the approval that authorized
                        # an order that may already have reached KIS.
                        result = execution_engine.submit_buy_order(
                            order_intent=order_intent, buy_gate_context_builder=_buy_ctx_builder,
                            conn=conn, broker=broker, instrument=instrument,
                            account_id=account_snapshot.account_id, now=current,
                            audit_run_id=run_id,
                        )
                    # A REJECTED transport result is not a submission.
                    #
                    # `submit_buy_order` PERSISTS the broker's answer and
                    # RETURNS it -- ACCEPTED, REJECTED or UNKNOWN -- and
                    # raises only when something went wrong on the way.
                    # This branch read "no exception" as "the order is
                    # live", so a rejection by KIS was recorded as an
                    # approved buy: counted in `submitted`, audited as
                    # APPROVED, and given an S6 position row at SUBMITTED
                    # for an order that does not exist anywhere.
                    #
                    # That row then blocked every later entry for the
                    # symbol through SYMBOL_ALREADY_HELD -- which is why
                    # the damage stopped at one order per symbol, and why
                    # it looked like duplicate protection working rather
                    # than a defect being contained by it. On 2026-08-27
                    # BTG, PBR and PTEN each ended the session holding a
                    # position the account never had.
                    if str(result.status).upper() == "REJECTED":
                        broker_record = result.execution_record
                        reason = (f"KIS rejected the order "
                                  f"(code={getattr(broker_record, 'error_code', None)!r}: "
                                  f"{getattr(broker_record, 'error_message', None)})")
                        logger.warning("BROKER_REJECTED %s qty=%s limit=%s %s",
                                       symbol, order_intent.quantity,
                                       order_intent.limit_price, reason)
                        results["blocked"].append((symbol, reason))
                        shadow_mode.persist(_shadow_record("REJECTED", reason))
                        outcome["result"] = shadow_audit.RESULT_BLOCKED
                        outcome["reason_code"] = "BROKER_REJECTED"
                        outcome["detail"] = reason
                        _audit(run_id, shadow_audit.GATE_REJECTED,
                               shadow_audit.RESULT_BLOCKED, symbol=symbol,
                               signal_id=signal.signal_id,
                               internal_order_id=order_intent.internal_order_id,
                               reason_code="BROKER_REJECTED", detail=reason,
                               now=current)
                        continue
                    # UNKNOWN is the other answer that is not a
                    # submission, and it is the dangerous one: the order
                    # may be live at KIS. It keeps the S6 row -- without
                    # it a held share would be invisible to the exit
                    # runtime -- but it is not counted or audited as an
                    # approved buy, and nothing here re-sends it.
                    if str(result.status).upper() == "UNKNOWN":
                        reason = ("KIS did not confirm the order; left UNKNOWN "
                                  "for reconciliation against KIS order history")
                        logger.warning("BROKER_UNKNOWN %s qty=%s limit=%s",
                                       symbol, order_intent.quantity,
                                       order_intent.limit_price)
                        results["blocked"].append((symbol, reason))
                        shadow_mode.persist(_shadow_record("AMBIGUOUS", reason))
                        outcome["result"] = shadow_audit.RESULT_ERROR
                        outcome["reason_code"] = "AMBIGUOUS_RESPONSE"
                        outcome["detail"] = reason
                        if s6_entry_lifecycle.is_s6(order_intent.strategy_id):
                            try:
                                s6_entry_lifecycle.record_entry_submission(
                                    conn, symbol=symbol,
                                    session=order_intent.session,
                                    client_order_id=order_intent.internal_order_id,
                                    candidate_row=_s6_candidate_row(source, symbol),
                                    now=current)
                            except Exception:  # noqa: BLE001
                                logger.exception(
                                    "S6 entry could not be recorded after an "
                                    "UNKNOWN response for %s -- reconcile "
                                    "against KIS order history before any "
                                    "further entry", symbol)
                        continue
                    accepted_at = datetime.now(timezone.utc)
                    _record_slippage(
                        symbol=symbol, side="buy", session=order_intent.session,
                        strategy_id=order_intent.strategy_id,
                        signal_price=signal.signal_price,
                        gate_price=kis_quote.price_usd,
                        order_price=order_intent.limit_price,
                        qty_requested=order_intent.quantity,
                        internal_order_id=order_intent.internal_order_id,
                        submit_at=submit_at, accepted_at=accepted_at,
                        broker_order_id=getattr(result.execution_record,
                                                "broker_order_id", None),
                        now=current)
                    results["submitted"].append(symbol)
                    shadow_mode.persist(_shadow_record("APPROVED"))
                    # GATE_APPROVED and EXECUTION_PLANNED were already
                    # recorded by the engine, before the transport call.
                    outcome["result"] = shadow_audit.RESULT_APPROVED
                    outcome["reason_code"] = "APPROVED"
                    # spec: "매수 체결 이후 포지션 관리는 KIS 실제 보유수량과
                    # 평균체결가를 기준으로 한다" -- create the positions/
                    # lifecycle.py row now so kis_position_manager.py's sync
                    # cycle can pick up the fill and start managing stop/
                    # target/time/EOD exits (see kis_position_manager.py's
                    # module docstring for the full rationale).
                    try:
                        if s6_entry_lifecycle.is_s6(order_intent.strategy_id):
                            # S6 records in ITS OWN canonical store and
                            # nowhere else. `strategy_registry.
                            # POSITION_TABLES` maps S6 to `s6_positions`,
                            # which is what `entry_limits` reads to decide
                            # whose a held position is -- a position absent
                            # from it is `unattributed`, and unattributed
                            # symbols count against EVERY slot.
                            #
                            # Not ALSO `positions`: that lifecycle runs its
                            # own stop/target/time/EOD exits while S6 has
                            # its own policy, and one position with two
                            # exit engines is worse than either.
                            s6_entry_lifecycle.record_entry_submission(
                                conn, symbol=symbol,
                                session=order_intent.session,
                                client_order_id=order_intent.internal_order_id,
                                candidate_row=_s6_candidate_row(source, symbol),
                                now=current)
                        else:
                            kis_position_manager.create_kis_position_after_buy(
                                strategy_id=order_intent.strategy_id, strategy_version="v1", symbol=symbol,
                                quantity=order_intent.quantity, client_order_id=order_intent.internal_order_id,
                                broker_order_id=result.execution_record.broker_order_id, now=current,
                            )
                    except Exception as exc:
                        # Position tracking failure must never be treated as
                        # order failure -- the KIS order already succeeded.
                        # Surfaced via results["blocked"] as a warning entry so
                        # it's visible, but the symbol stays in "submitted".
                        results["blocked"].append((symbol, f"WARNING: position tracking failed after successful buy: {exc}"))
                except ExecutionLockUnavailable as exc:
                    # Something else holds execution access -- an exit, a
                    # runtime tick, reconciliation. All of them outrank a
                    # new entry, so this drops the prepared order rather
                    # than queue behind them. Nothing was sent.
                    reason = ("execution access is held by another cycle; "
                              "this entry was dropped before submission")
                    logger.info("ENTRY_LOCK_UNAVAILABLE %s: %s", symbol, exc)
                    results["blocked"].append((symbol, reason))
                    outcome["result"] = shadow_audit.RESULT_BLOCKED
                    outcome["reason_code"] = "EXECUTION_LOCK_UNAVAILABLE"
                    outcome["detail"] = reason
                    _audit(run_id, shadow_audit.GATE_REJECTED,
                           shadow_audit.RESULT_BLOCKED, symbol=symbol,
                           signal_id=signal.signal_id,
                           internal_order_id=order_intent.internal_order_id,
                           reason_code="EXECUTION_LOCK_UNAVAILABLE",
                           detail=reason, now=current)
                    continue
                except ExecutionEngineError as exc:
                    results["blocked"].append((symbol, str(exc)))
                    shadow_mode.persist(_shadow_record("BLOCKED", str(exc)))
                    outcome["reason_code"] = exc.reason_code or "GATE"
                    _audit(run_id, shadow_audit.event_type_for_reason_code(exc.reason_code),
                           shadow_audit.RESULT_BLOCKED, symbol=symbol, signal_id=signal.signal_id,
                           internal_order_id=order_intent.internal_order_id,
                           reason_code=exc.reason_code, detail=str(exc), now=current)
                    # A same-day re-entry block is the one refusal whose
                    # correctness is not knowable at the time: it stops a
                    # trade, and only the price afterwards says whether
                    # stopping it was right. Recorded with the candidate
                    # that was refused so §N can answer that later.
                    _record_reentry_block(
                        conn, exc, signal=signal, symbol=symbol,
                        order_intent=order_intent, now=current)
                except KISAmbiguousResponseError as exc:
                    results["blocked"].append((symbol, f"ambiguous KIS response, order status UNKNOWN: {exc}"))
                    shadow_mode.persist(_shadow_record("AMBIGUOUS", str(exc)))
                    # The terminal SHADOW_ERROR is written once, by the
                    # finally-block below. Writing it here as well gave
                    # this run TWO terminal events.
                    outcome["result"] = shadow_audit.RESULT_ERROR
                    outcome["reason_code"] = "AMBIGUOUS_RESPONSE"
                    outcome["detail"] = str(exc)
                    # An ambiguous response is the case this row exists
                    # for. The order may be live at KIS; without a
                    # SUBMITTED row there would be a share held against
                    # nothing internal, invisible to the exit runtime and
                    # unattributable by the cap. `sync_buy_fills` settles
                    # it from broker evidence -- opening it if it filled,
                    # abandoning it only if the broker positively reports
                    # it never did. Nothing here re-sends the order.
                    if s6_entry_lifecycle.is_s6(order_intent.strategy_id):
                        try:
                            s6_entry_lifecycle.record_entry_submission(
                                conn, symbol=symbol,
                                session=order_intent.session,
                                client_order_id=order_intent.internal_order_id,
                                candidate_row=_s6_candidate_row(source, symbol),
                                now=current)
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "S6 entry could not be recorded after an "
                                "AMBIGUOUS response for %s -- reconcile "
                                "against KIS order history before any "
                                "further entry", symbol)
                except KISBrokerError as exc:
                    results["blocked"].append((symbol, f"KIS order rejected: {exc}"))
                    shadow_mode.persist(_shadow_record("REJECTED", str(exc)))
                    outcome["reason_code"] = "BROKER_REJECTED"
                    _audit(run_id, shadow_audit.GATE_REJECTED, shadow_audit.RESULT_BLOCKED, symbol=symbol,
                           signal_id=signal.signal_id,
                           internal_order_id=order_intent.internal_order_id,
                           reason_code="BROKER_REJECTED", detail=str(exc), now=current)
            except shadow_audit.ShadowAuditFailure as exc:
                # handle_audit_failure() already recorded the terminal
                # SHADOW_ERROR and alerted. Do not write a second terminal
                # event for this run.
                terminal_recorded = True
                results["blocked"].append((symbol, f"shadow audit failure: {exc}"))
            except FatalRepositoryConnectionError:
                # CODEX-059: a fatal connection fault aborts the WHOLE
                # cycle -- no further symbols are evaluated -- and reaches
                # the entrypoint unchanged so the process fail-stops.
                terminal_recorded = True
                _finalize(run_id, {"result": shadow_audit.RESULT_ERROR,
                                   "reason_code": "FATAL_REPOSITORY_CONNECTION",
                                   "detail": None}, symbol=symbol, now=current)
                raise
            except idempotency.IdempotencyLockBusy as exc:
                # Two processes registering orders on one account at the
                # same instant. Transient, candidate-specific, and it
                # says nothing about this candidate -- filing it as
                # UNEXPECTED made three ordinary collisions look like
                # defects. The next tick re-evaluates it.
                reason = f"{IDEMPOTENCY_LOCK_BUSY}: {exc}"
                logger.info("%s for %s; the next tick re-evaluates it",
                            IDEMPOTENCY_LOCK_BUSY, symbol)
                results["blocked"].append((symbol, reason))
                outcome = {"result": shadow_audit.RESULT_BLOCKED,
                           "reason_code": IDEMPOTENCY_LOCK_BUSY,
                           "detail": str(exc)[:200]}
            except Exception as exc:  # noqa: BLE001 -- audited, then reported as a blocked result
                outcome = {"result": shadow_audit.RESULT_ERROR, "reason_code": "UNEXPECTED"}
                results["blocked"].append((symbol, f"unexpected error: {exc}"))
            finally:
                if not terminal_recorded:
                    _finalize(run_id, outcome, symbol=symbol, now=current)
    finally:
        conn.close()

    return results
