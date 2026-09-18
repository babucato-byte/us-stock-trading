"""S6 exit decisions turned into real SELLs, through the shared path.

What is shared and what is not
------------------------------
The DECISION is S6's: `s6_live.exit_policy` owns when an S6 position
leaves, and no other strategy's exit ever sees one. The SUBMISSION is
`s1_live.exit_runtime._submit_sell` with S6's store passed in -- the same
function S2 uses, carrying behaviours learned the hard way on S1: an
ambiguous send goes to SUBMISSION_UNKNOWN and is never auto-retried, a
rejection does not chase the price or enlarge the quantity, and the exit
intent ledger refuses a second order for a position that already has one
live.

A third copy of that would be a third idea of what is safe, and three
ideas diverge faster than two.

Fill synchronisation is not exit ownership
------------------------------------------
`sync_buy_fills` and `sync_sell_fills` are here but are deliberately NOT
gated by anything to do with who owns the exit. Conflating the two is
what cost S1 its bookkeeping once: an exit guard excluded a strategy
wholesale and took fill synchronisation with it, so the position stopped
being counted while still being held.

Exits are never gated by entry risk
-----------------------------------
No allocator, no position limit, no reconciliation check. A control that
also blocked liquidation would trap the account in the position it exists
to escape. Entry fail-closed and exit continuity are different rules.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from execution import execution_lock
from execution.execution_lock import ExecutionLockUnavailable
from s1_live.exit_runtime import (
    _accepts_execution_lock,
    ACTION_BLOCKED as _ACTION_BLOCKED,
    ACTION_LATCHED as _ACTION_LATCHED,
    ExitOutcome,
    _submit_sell,
)
from s6_live import exit_diagnostics, exit_policy, position_store

logger = logging.getLogger(__name__)

#: Named so `EXEC_LOCK owner=...` says which side of the book took it.
_EXEC_LOCK_OWNER_EXIT = "S6_EXIT_SUBMIT"
#: Reason code for the under-lock row re-check refusing a SELL.
_SELL_PRECHECK_REASON_CODE = "SELL_PRECHECK_FAILED"

#: Statuses from which a SELL may still be sent. SUBMITTED is excluded on
#: purpose: that row has no fill behind it yet, so there is nothing to
#: sell. CLOSED and EXIT_SUBMITTED are already done or already leaving.
_SELLABLE_STATUSES = (position_store.OPEN, position_store.EXIT_PENDING)

#: The held symbol's market data could not be obtained at all this tick.
#: Distinct from a rule that read the data and said no: this is the
#: absence of evidence, and it must never be recorded as a calm market.
POSITION_DATA_UNAVAILABLE = "POSITION_DATA_UNAVAILABLE"

#: A live fill inquiry answered, and answered zero, while the book still
#: holds shares. Visible on purpose: this is the state MTCH was stuck in.
SELL_FILL_REPORTS_ZERO = "SELL_FILL_REPORTS_ZERO"

#: The broker has no position and no open order, but this system cannot
#: independently confirm its own SELL executed. Deliberately NOT the
#: normal CLOSED-with-a-price path: KIS returned full execution detail
#: for every other S6 sell in the window and none for this order, so
#: "the sell filled" is a claim only our ledger makes.
#:
#: Recorded with no exit price, which is also how realised PnL stays
#: unknown -- `s6_positions` derives PnL from entry and exit price, so a
#: NULL exit price is an absent result rather than a breakeven one.
EXTERNALLY_CLOSED_SELL_UNCONFIRMED = "EXTERNALLY_CLOSED_SELL_UNCONFIRMED"

ACTION_HELD = "HELD"
ACTION_SOLD = "SOLD"
ACTION_BLOCKED = "BLOCKED"
ACTION_LATCHED = "LATCHED"

CLIENT_ORDER_PREFIX = "s6exit"


def _finite(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


#: Ledger statuses that mean the order will never fill, ever.
#:
#: A rejected order has no broker order id, so the fill lookup has
#: nothing to ask KIS about and answers "no fills yet" forever. The row
#: then sits at SUBMITTED, counts against the symbol lock, and blocks
#: that symbol for the rest of the session -- which is exactly what
#: happened to BTG, PBR and PTEN on 2026-08-27.
#:
#: A rejection is STRONGER evidence than an empty fill lookup, not
#: weaker: the broker positively refused the order. Only REJECTED and
#: CANCELLED qualify. UNKNOWN never does -- that is the state that
#: exists precisely because nobody knows.
_TERMINAL_LEDGER_STATUSES = ("REJECTED", "CANCELLED")


def _order_will_never_fill(conn, row):
    """True when the order ledger says this row's order was refused."""
    client_order_id = row.get("client_order_id")
    if not client_order_id:
        return False
    try:
        found = conn.execute(
            "SELECT status FROM kis_order_idempotency "
            "WHERE internal_order_id = ?", (client_order_id,)).fetchone()
    except Exception:  # noqa: BLE001 - an unreadable ledger is not
        # evidence of anything, and must never abandon a live row.
        logger.warning("S6 could not read the order ledger for %s",
                       client_order_id, exc_info=True)
        return False
    if found is None:
        return False
    status = str(found["status"] if hasattr(found, "keys") else found[0] or "")
    return status.upper() in _TERMINAL_LEDGER_STATUSES


def sync_buy_fills(conn, *, fills_for, now=None) -> List[Dict[str, Any]]:
    """SUBMITTED -> OPEN from the broker's own fills.

    `fills_for(row)` returns {"filled_quantity", "average_fill_price",
    "venue", "order_id"} or None. Cumulative quantity is what is applied,
    so a fill seen twice is a no-op and a stale smaller one is ignored --
    both decided in the store, not here.

    An order the broker reports as never filled is ABANDONED rather than
    left SUBMITTED forever: a row that can never resolve is
    indistinguishable from one still in flight, and the position limit
    counts both.
    """
    # SUBMITTED rows AND already-open ones. A BUY that fills in two
    # parts reaches OPEN on the first fill and then stops being
    # "unconfirmed", so scanning only SUBMITTED would leave the position
    # permanently short of what the account actually holds -- and
    # reconciliation would report the difference as an unattributable
    # broker holding. `apply_fill` compares CUMULATIVE quantity, so
    # re-reading a completed fill is a no-op.
    pending = list(position_store.load_unconfirmed(conn))
    pending += [row for _pid, row in position_store.load_live(conn)
                if not row.get("exit_submitted")]

    applied = []
    for row in pending:
        pid, symbol = row["position_id"], row["symbol"]
        # Asked BEFORE the broker lookup, because for a refused order
        # there is nothing to look up: no broker order id was ever
        # issued, so the lookup returns "no fills yet" every time and the
        # row can never reach a terminal state on its own.
        if (row.get("status") == position_store.SUBMITTED
                and _order_will_never_fill(conn, row)):
            position_store.abandon_submission(
                conn, pid, reason="BUY_NEVER_FILLED", now=now)
            logger.info("S6 abandoned %s (%s): the order ledger reports it "
                        "was refused, so it can never fill", pid, symbol)
            applied.append({"position_id": pid, "symbol": symbol,
                            "status": "ABANDONED"})
            continue
        try:
            fill = fills_for(row)
        except Exception as exc:  # noqa: BLE001 - one symbol's lookup
            # failing must not cost the others their synchronisation.
            logger.error("S6 BUY fill lookup failed for %s", symbol,
                         exc_info=True)
            applied.append({"position_id": pid, "symbol": symbol,
                            "error": str(exc)})
            continue

        if not fill:
            applied.append({"position_id": pid, "symbol": symbol,
                            "status": "STILL_UNCONFIRMED"})
            continue

        quantity = fill.get("filled_quantity")
        if not quantity:
            # Explicitly reported as unfilled -- different from "no
            # answer yet", and the only case safe to abandon. Only a
            # SUBMITTED row can be abandoned: an OPEN position holds
            # shares, and "no fill rows" for it means the lookup found
            # nothing, never that the shares are not there.
            if fill.get("terminal") and row.get("status") == position_store.SUBMITTED:
                position_store.abandon_submission(
                    conn, pid, reason="BUY_NEVER_FILLED", now=now)
                applied.append({"position_id": pid, "symbol": symbol,
                                "status": "ABANDONED"})
            continue

        changed = position_store.apply_fill(
            conn, pid, filled_quantity=quantity,
            average_fill_price=fill.get("average_fill_price"),
            venue=fill.get("venue"), entry_order_id=fill.get("order_id"),
            now=now)
        applied.append({"position_id": pid, "symbol": symbol,
                        "status": "OPENED" if changed else "NO_CHANGE",
                        "filled_quantity": quantity})
        if changed:
            try:
                fill_at = (fill.get("broker_fill_time") or
                           fill.get("filled_at") or now or
                           datetime.now(timezone.utc))
                if hasattr(fill_at, "isoformat"):
                    fill_at = fill_at.isoformat()
                conn.execute(
                    "UPDATE order_lineage SET fill_at = COALESCE(fill_at, ?), "
                    "broker_fill_time = COALESCE(broker_fill_time, ?) "
                    "WHERE position_id = ?",
                    (fill_at, fill_at, pid))
                conn.commit()
            except Exception:  # noqa: BLE001 - fill is already durable
                logger.warning("S6 fill lineage not completed for %s", symbol,
                               exc_info=True)
            # The one human-facing BUY message, after the durable write.
            # Side effect only; the notifier never raises and its result
            # is not read. Keyed on the position so a second tick that
            # sees the same fill cannot announce it twice.
            _announce_buy_fill(conn, row, pid, symbol, quantity, fill)
    return applied


def _announce_buy_fill(conn, row, pid, symbol, quantity, fill) -> None:
    try:
        from operations import live_notifications as ln

        fields = ln.fill_completed_fields(
            symbol=symbol, filled_qty=quantity,
            fill_price=fill.get("average_fill_price"),
            position_qty=quantity, average_cost=fill.get("average_fill_price"),
            strategy_id=row.get("strategy_id"),
            session=row.get("entry_session"),
            broker_order_id=fill.get("order_id"))
        fields.update(_fill_quality_context(row))
        ln.notify(ln.FILL_COMPLETED, fields,
                  dedupe_conn=conn, dedupe_subject=pid, dedupe_version="OPENED")
    except Exception:  # noqa: BLE001 - a message must never touch the book
        logger.warning("S6 BUY fill notice for %s not sent", symbol, exc_info=True)


def _fill_quality_context(row) -> Dict[str, Any]:
    """The compact ORB / freshness context for the one fill message:
    the range length and, when the decision snapshot exists, the breakout
    age and whether recent volume was holding. Presentation only."""
    import json

    out: Dict[str, Any] = {}
    try:
        if row.get("range_minutes") is not None:
            out["orb_minutes"] = int(row["range_minutes"])
        raw = row.get("entry_quality_json")
        if raw:
            snapshot = json.loads(raw) if isinstance(raw, str) else dict(raw)
            if snapshot.get("breakout_age_minutes") is not None:
                out["breakout_age_minutes"] = snapshot["breakout_age_minutes"]
            decay = snapshot.get("volume_decay")
            if decay is not None:
                out["recent_volume_state"] = "감소" if decay else "유지"
    except Exception:  # noqa: BLE001 - context is optional
        pass
    return out


def _announce_sell_fill(conn, row, pid, symbol, sold, fill, *, session,
                        remaining) -> None:
    try:
        from operations import live_notifications as ln

        ln.notify(
            ln.SELL_FILLED,
            ln.sell_filled_fields(
                symbol=symbol, qty=sold, fill_price=fill.get("average_fill_price"),
                realized_pnl=None, realized_pnl_pct=None,
                position_after=remaining, reason=row.get("exit_reason"),
                strategy_id=row.get("strategy_id"),
                session=_session_name(session) or row.get("exit_session"),
                average_buy_price=row.get("entry_price")),
            dedupe_conn=conn, dedupe_subject=pid,
            dedupe_version=f"SOLD:{sold}:{remaining}")
    except Exception:  # noqa: BLE001 - a message must never touch the book
        logger.warning("S6 SELL fill notice for %s not sent", symbol, exc_info=True)


def _session_name(session):
    """A session object, a plain string, or None -- all acceptable."""
    if session is None:
        return None
    return getattr(session, "name", None) or (
        str(session) if isinstance(session, str) else None)


def _record_broker_fill_time(conn, position_id, broker_timestamp):
    """KIS's own execution time, alongside the tick-stamped close.

    Research bookkeeping only, and never fatal: the position is already
    closed by the time this runs.
    """
    try:
        from post_exit import tracker

        tracker.record_broker_fill_time(
            conn, position_id=position_id, broker_timestamp=broker_timestamp)
    except Exception:  # noqa: BLE001
        logger.debug("broker fill time not recorded for %s", position_id,
                     exc_info=True)


def _settle_intent(conn, position_id, sold, *, done):
    """Close the exit intent out once the fill that answers it is in.

    The position book and the intent ledger are two records of one exit
    and only the position book was being closed. The intent stayed
    non-terminal forever -- which reads downstream as an exit still in
    flight, one of the ambiguities that fails closed, on a position whose
    shares are already gone.

    Never fatal: the fill is applied and the position is closed by the
    time this runs, and losing the ledger's copy of that must not undo
    it. A missing or already-terminal intent is simply nothing to do.
    """
    from state_store import exit_intent_ledger as eil

    try:
        intent = eil.get_active_intent(conn, position_id)
        if not intent:
            return None
        if done:
            eil.mark_confirmed(conn, intent["intent_id"],
                               confirmed_filled_qty=sold)
        else:
            eil.update_progress(conn, intent["intent_id"], sold)
        return intent
    except Exception:  # noqa: BLE001
        logger.warning("S6 could not settle the exit intent for %s; the "
                       "position itself is closed", position_id,
                     exc_info=True)
        return None


def sync_sell_fills(conn, *, fills_for, session=None, now=None) -> List[Dict[str, Any]]:
    """EXIT_SUBMITTED -> CLOSED, or a reduced position on a partial.

    A partial SELL leaves the remainder OPEN and still managed. Closing
    on a partial would orphan shares the broker still holds -- the
    position would vanish from the strategy while the account kept the
    risk.
    """
    results = []
    for pid, row in position_store.load_live(conn):
        if not row.get("exit_submitted"):
            continue
        symbol = row["symbol"]
        try:
            fill = fills_for(row)
        except Exception as exc:  # noqa: BLE001
            logger.error("S6 SELL fill lookup failed for %s", symbol,
                         exc_info=True)
            results.append({"position_id": pid, "symbol": symbol,
                            "error": str(exc)})
            continue
        if not fill:
            results.append({"position_id": pid, "symbol": symbol,
                            "status": "AWAITING_SELL_FILL"})
            continue

        sold = int(fill.get("filled_quantity") or 0)
        held = int(row.get("quantity") or 0)
        if sold <= 0:
            # Was a bare `continue`. MTCH sat EXIT_SUBMITTED for five and a
            # half hours on 2026-09-01 taking exactly this branch about
            # fifty times: the inquiry answered usable-but-NO_FILL, the row
            # produced no result, no log and no escalation, and every S6
            # entry deferred behind it. A retry that cannot say it is
            # retrying is indistinguishable from nothing happening.
            #
            # Deliberately does NOT close anything. A live inquiry
            # reporting zero is not evidence of a fill; it is evidence
            # that this tick learned nothing.
            results.append({
                "position_id": pid, "symbol": symbol,
                "status": SELL_FILL_REPORTS_ZERO,
                "broker_order_id": fill.get("order_id"),
                "inquiry_status": fill.get("status"),
                "inquiry_filled_quantity": sold,
                "held_quantity": held,
            })
            logger.warning(
                "S6 %s: %s -- inquiry says %s (filled=%s) while the book "
                "still holds %s; position stays %s",
                symbol, SELL_FILL_REPORTS_ZERO, fill.get("status"), sold,
                held, row.get("status"))
            continue
        # `sold` is the broker's CUMULATIVE total for this exit; `held` is
        # what the position has LEFT after any earlier partial was applied.
        # Comparing them directly closed the position the moment the
        # running total reached the remainder -- 7 of 10 sold against a
        # row already reduced to 5 read as "fully filled" and closed it
        # with three shares still in the account. The increment is what
        # this tick learned, and it is what answers the remainder.
        intent = _active_intent(conn, pid)
        delta = max(sold - filled_before_abort(intent), 0)
        if delta >= held:
            # The broker's own average fill, carried through instead of
            # discarded: it is the only price at which the trade actually
            # ended, and nothing downstream can recover it later.
            # The session is recorded by the tick that saw the fill --
            # a REGULAR entry closed in AFTER_HOURS is a different trade
            # from one closed in REGULAR, and deriving it later from
            # `closed_at` guesses at what this moment already knows.
            position_store.close_position(
                conn, pid, reason=row.get("exit_reason"),
                exit_price=fill.get("average_fill_price"),
                exit_session=_session_name(session), now=now)
            settled = _settle_intent(conn, pid, sold, done=True)
            if settled:
                # The broker fill that closed the position is direct,
                # positive execution evidence.  Keep the idempotency
                # projection terminal too; failure here never reopens the
                # position or causes a new SELL.
                from reconciliation import sell_projection

                sell_projection.settle_confirmed_sell(
                    conn, client_order_id=settled.get("client_order_id"),
                    broker_order_id=settled.get("broker_order_id") or fill.get("order_id"),
                    confirmed_filled_qty=sold,
                    expected_quantity=settled.get("requested_qty"),
                    event_type="SELL_FILL_SYNCED",
                    evidence="broker_fill_sync", now=now,
                )
            _record_broker_fill_time(conn, pid, fill.get("broker_timestamp"))
            results.append({"position_id": pid, "symbol": symbol,
                            "status": "CLOSED", "sold": sold,
                            "exit_price": fill.get("average_fill_price")})
            _announce_sell_fill(conn, row, pid, symbol, sold, fill,
                                session=session, remaining=0)
        else:
            # Was an inline UPDATE of `held - sold` plus a separate
            # progress write. `sold` is the broker's CUMULATIVE total, so
            # observing the same partial on a later tick subtracted it
            # again -- this path double-applied on its own, before any
            # recovery path was involved.
            apply_confirmed_exit_fill(conn, pid, intent, sold, now=now)
            fresh = position_store.load(conn, pid) or {}
            remaining = int(fresh.get("quantity") or 0)
            results.append({"position_id": pid, "symbol": symbol,
                            "status": "PARTIALLY_SOLD", "sold": sold,
                            "remaining": remaining})
            _announce_sell_fill(conn, row, pid, symbol, sold, fill,
                                session=session, remaining=remaining)
    return results


def _sell_still_valid(fresh) -> Optional[str]:
    """Is this row still one a SELL may be sent for? Detail string if not.

    Read UNDER the execution lock, immediately before submitting. The
    decision that got us here was made outside the lock -- that is the
    point of the change -- so the row it was made from may be minutes
    old by now, and a fill sync or another cycle may have moved it.

    Deliberately a PRE-CHECK, not a second authority. The ledger's
    `reserve` is still what makes a duplicate SELL impossible; this
    refuses the obvious cases early, with a reason a human can read,
    rather than letting them surface as a DuplicateExitIntentError.
    """
    if fresh is None:
        return "position row no longer exists"
    status = fresh.get("status")
    if status == position_store.CLOSED:
        return "position closed while waiting for the execution lock"
    if status == position_store.EXIT_SUBMITTED:
        return "exit already submitted while waiting for the execution lock"
    if status not in _SELLABLE_STATUSES:
        return f"position status {status!r} cannot be sold"
    if fresh.get("exit_submitted"):
        return "exit already submitted while waiting for the execution lock"
    try:
        quantity = int(fresh.get("quantity") or 0)
    except (TypeError, ValueError):
        return f"unreadable quantity {fresh.get('quantity')!r}"
    if quantity < 1:
        return f"no quantity to sell (quantity={quantity})"
    return None


def _submit_sell_locked(conn, *, broker_adapter, position_id, row, reason,
                        now=None) -> ExitOutcome:
    """Take the execution lock, revalidate, and submit inside it.

    Why the lock is here and not around the evaluation
    --------------------------------------------------
    Evaluating an exit reads bars and computes indicators; it mutates
    nothing. Submitting one mutates the broker, the intent ledger and the
    position row, and THAT is what must not interleave with a BUY.

    The cron wrapper used to hold this lock for the whole runtime, so a
    216-second evaluation blocked the next three one-minute ticks and the
    monitor starved itself: 79.6% of ticks skipped, 38 minutes between
    evaluations at worst. Entry was given this exact treatment on
    2026-09-02 and the reasoning carries over unchanged -- what is no
    longer serialised is the ANALYSIS, which never mutated anything.

    A lock it cannot take is never a reason to sell anyway, and never a
    terminal state: the exit LATCHES, which is the same mechanism a
    session that cannot place orders already uses, and
    `retry_latched_exits` sends it on the next tick.
    """
    symbol = row["symbol"]

    # The lock moved INTO the engine, around the decision and the write.
    #
    # Held here it wrapped the engine's own reconciliation reads -- three
    # venue sweeps and a paged fill history -- exactly as the entry path did
    # before LKQ cost it 213.8 seconds on 2026-09-17. Those reads are still
    # the engine's, taken by the engine, as CODEX-044 requires; only the
    # moment the lock is taken has changed.
    #
    # `_sell_still_valid` travels with it as `pre_submit_check`, so the row
    # is re-read and re-judged under the lock, which is the only place that
    # question means anything. A lock that cannot be taken, or a snapshot
    # that went stale while waiting for it, now surfaces as a blocked
    # submission -- which `_submit_sell` already handles by aborting the
    # intent and LATCHING, the same transient retry this function used to
    # perform itself.
    def _still_sellable():
        fresh_row = position_store.load(conn, position_id)
        blocked = _sell_still_valid(fresh_row)
        if not blocked:
            return None
        logger.warning(
            "S6 %s: SELL abandoned after taking the execution lock -- %s",
            symbol, blocked)
        return (_SELL_PRECHECK_REASON_CODE, blocked)

    if not _accepts_execution_lock(broker_adapter):
        # An adapter that cannot carry the lock keeps the ORIGINAL shape:
        # this function takes the lock and runs the re-check itself.
        #
        # Deliberately not a silent degrade. Handing the check to an adapter
        # that drops it would remove a refusal rather than weaken one -- a
        # closed position would be sold -- so the capability probe falls
        # back to the behaviour it is replacing instead of to none.
        try:
            with execution_lock.hold(_EXEC_LOCK_OWNER_EXIT):
                fresh = position_store.load(conn, position_id)
                blocked = _sell_still_valid(fresh)
                if blocked:
                    logger.warning(
                        "S6 %s: SELL abandoned after taking the execution lock "
                        "-- %s", symbol, blocked)
                    return ExitOutcome(position_id, symbol, _ACTION_BLOCKED,
                                       reason, blocked)
                return _submit_sell(
                    conn, broker_adapter=broker_adapter, position_id=position_id,
                    row=fresh, reason=reason, now=now, store=position_store,
                    prefix=CLIENT_ORDER_PREFIX)
        except ExecutionLockUnavailable as exc:
            position_store.latch_pending_exit(conn, position_id, reason, now=now)
            logger.warning(
                "S6 %s: execution lock unavailable, exit LATCHED for the next "
                "tick rather than submitted: %s", symbol, exc)
            return ExitOutcome(position_id, symbol, _ACTION_LATCHED, reason,
                               f"execution lock unavailable: {exc}")

    fresh = position_store.load(conn, position_id) or row
    return _submit_sell(
        conn, broker_adapter=broker_adapter, position_id=position_id,
        row=fresh, reason=reason, now=now, store=position_store,
        prefix=CLIENT_ORDER_PREFIX,
        execution_lock_owner=_EXEC_LOCK_OWNER_EXIT,
        pre_submit_check=_still_sellable)


def evaluate_position(conn, *, broker_adapter, position_id, row,
                      features=None, current_price=None, session=None,
                      now=None, orders_allowed=True,
                      emergency=False) -> ExitOutcome:
    """Decide, and submit if the decision is SELL.

    The observation is recorded BEFORE the decision, so the peaks the
    decision reads include this tick. Asking first would judge a position
    against a peak it had already exceeded.
    """
    symbol = row["symbol"]
    position_store.observe(
        conn, position_id, price=current_price,
        volume_expansion=_finite(getattr(features, "volume_expansion", None)),
        now=now)
    refreshed = position_store.load(conn, position_id) or row

    state = position_store.to_state(refreshed)
    decision = exit_policy.decide(
        state, current_price=current_price,
        features=features, session=session, now=now, emergency=emergency)

    # Every tick records what each rule answered, including the ones that
    # could not answer at all. A HOLD that was really "three rules had no
    # VWAP to read" must not look like a calm market -- see
    # s6_live/exit_diagnostics.py.
    diagnostics = exit_diagnostics.evaluate(
        state, features=features, price=exit_policy._price_of(
            features, current_price),
        session=session, now=now, decision=decision)
    if diagnostics.get("unavailable_rules"):
        # Named explicitly when NOTHING could be read, because "every
        # rule abstained" and "the market was calm" are the same silence
        # otherwise -- and on a position holding real money they call for
        # opposite responses. JBS was held live on 2026-09-01 with no
        # stream subscription at all; REGULAR's provider fallback covered
        # it, an extended session would not have.
        whole_view_missing = features is None or not _finite(
            getattr(features, "price", None))
        logger.warning(
            "S6 %s: %s%d exit rule(s) could not be evaluated this tick: %s",
            symbol,
            (POSITION_DATA_UNAVAILABLE + " -- ") if whole_view_missing else "",
            len(diagnostics["unavailable_rules"]),
            ", ".join(diagnostics["unavailable_rules"]))
        diagnostics["position_data_unavailable"] = bool(whole_view_missing)

    # Observation only, and AFTER the decision exists so it cannot be part
    # of one. A profit-protection exit is under measurement, not in force:
    # see s6_live/profit_protection_shadow.py. Wrapped because a research
    # record must never be able to stop a position from leaving.
    try:
        from market_hours import us_trading_day
        from s6_live import profit_protection_shadow as pps

        pps.observe(
            position_id=position_id, symbol=symbol,
            session=_session_name(session),
            trading_day=us_trading_day(now),
            now=now, entry_price=refreshed.get("entry_price"),
            price=exit_policy._price_of(features, current_price),
            peak_price=refreshed.get("peak_price"),
            ema9=_finite(getattr(features, "ema9", None)),
            decision_action=(exit_policy.SELL if decision.sells
                             else exit_policy.HOLD),
            decision_reason=decision.reason)
    except Exception:  # noqa: BLE001 - fail open: never block a real exit
        logger.warning("profit-protection shadow skipped for %s", symbol,
                       exc_info=True)

    if not decision.sells:
        # The diagnostics ARE the detail. An empty string here is what
        # made the DT hold unexplainable after the fact.
        return ExitOutcome(position_id, symbol, ACTION_HELD,
                           decision.reason, diagnostics)

    if not orders_allowed:
        # Latched, never dropped. A session that cannot place orders is a
        # reason to wait, not a reason to forget the position should be
        # leaving -- and §7 requires the retry on the next window.
        position_store.latch_pending_exit(conn, position_id, decision.reason,
                                          now=now)
        return ExitOutcome(position_id, symbol, ACTION_LATCHED,
                           decision.reason, "session does not permit orders")

    # A position that is ALREADY leaving sells for the reason it latched
    # on, not for whatever fired most recently.
    #
    # RIG latched EMA_STRUCTURE_FAILURE on 2026-08-28 at 19:52. It sold
    # three days later, and because this passed `decision.reason`, the
    # trade was recorded as RANGE_REENTRY -- the condition that happened
    # to fire on the tick that got the order out. The sale itself was
    # correct; the attribution was not, and post-exit analytics key on
    # (strategy_id, exit_reason), so the exit would have been studied
    # under a rule that did not cause it.
    #
    # The fresh decision still decides WHETHER to sell. It does not get
    # to relabel a decision already made.
    latched = (refreshed.get("pending_exit_reason")
               if refreshed.get("status") == position_store.EXIT_PENDING
               else None)
    if latched and latched != decision.reason:
        logger.info(
            "S6 %s: selling on the latched reason %s, not this tick's %s",
            symbol, latched, decision.reason)
    return _submit_sell_locked(conn, broker_adapter=broker_adapter,
                               position_id=position_id, row=refreshed,
                               reason=latched or decision.reason, now=now)


#: S-03: how many consecutive ticks a HELD position's exit evaluation may
#: fail (a data/API fault, not a HOLD decision) before it escalates to
#: the alert channel. Same bounded-alert shape as
#: `live_notifications.REPEATED_REJECTION_THRESHOLD` -- not silence, and
#: not one message per tick either.
EXIT_EVALUATION_FAILURE_THRESHOLD = 3

_EXIT_HEALTH_TABLE = "s6_exit_evaluation_health"


def _ensure_exit_health_table(conn) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_EXIT_HEALTH_TABLE} ("
        "position_id TEXT PRIMARY KEY, symbol TEXT, "
        "consecutive_failures INTEGER NOT NULL DEFAULT 0, updated_at TEXT)")


def _record_exit_evaluation_outcome(conn, position_id, symbol, *, ok, now=None) -> None:
    """Durable, per-position consecutive-failure count (S-03).

    A dedicated table rather than a column on `s6_positions`: this is
    purely notification bookkeeping and must never be read by, or
    influence, the exit DECISION itself. Never fatal -- a failure here
    must not cost the position its actual exit evaluation, which has
    already happened by the time this runs.
    """
    try:
        _ensure_exit_health_table(conn)
        row = conn.execute(
            f"SELECT consecutive_failures FROM {_EXIT_HEALTH_TABLE} WHERE position_id = ?",
            (position_id,)).fetchone()
        previous = int(row[0]) if row else 0
        current = now or datetime.now(timezone.utc)
        if ok:
            if previous >= EXIT_EVALUATION_FAILURE_THRESHOLD:
                _notify_exit_evaluation_transition(
                    conn, position_id, symbol, failing=False,
                    consecutive_failures=previous)
            if previous:
                conn.execute(
                    f"DELETE FROM {_EXIT_HEALTH_TABLE} WHERE position_id = ?",
                    (position_id,))
                conn.commit()
            return
        count = previous + 1
        conn.execute(
            f"INSERT INTO {_EXIT_HEALTH_TABLE} "
            "(position_id, symbol, consecutive_failures, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(position_id) DO UPDATE SET "
            "consecutive_failures = excluded.consecutive_failures, "
            "updated_at = excluded.updated_at",
            (position_id, symbol, count, current.isoformat()))
        conn.commit()
        if count == EXIT_EVALUATION_FAILURE_THRESHOLD:
            _notify_exit_evaluation_transition(
                conn, position_id, symbol, failing=True, consecutive_failures=count)
    except Exception:  # noqa: BLE001 -- bookkeeping must never affect the exit
        logger.warning("S6 exit-evaluation health tracking failed for %s",
                       symbol, exc_info=True)


def _notify_exit_evaluation_transition(conn, position_id, symbol, *, failing,
                                       consecutive_failures) -> None:
    try:
        from operations import live_notifications as ln

        event = ln.EXIT_EVALUATION_FAILING if failing else ln.EXIT_EVALUATION_RECOVERED
        fields = {"symbol": symbol, "consecutive_failures": consecutive_failures,
                  "action": "RECONCILE" if failing else "NONE"}
        ln.notify(event, fields, dedupe_conn=conn, dedupe_subject=position_id,
                  dedupe_version=f"{'FAILING' if failing else 'RECOVERED'}:{consecutive_failures}")
    except Exception:  # noqa: BLE001 -- a message must never touch the book
        logger.warning("S6 exit-evaluation health notice for %s not sent",
                       symbol, exc_info=True)


def run_exits(conn, *, broker_adapter, features_fn, price_fn, session=None,
              now=None, orders_allowed=True, emergency=False
              ) -> List[Dict[str, Any]]:
    """Every held S6 position, evaluated once.

    One position's failure does not cost the others theirs, and a failure
    is reported rather than dropped -- an exit that was never evaluated
    looks exactly like one that decided to hold.
    """
    outcomes = []
    for position_id, row in position_store.load_live(conn):
        symbol = row["symbol"]
        try:
            outcome = evaluate_position(
                conn, broker_adapter=broker_adapter, position_id=position_id,
                row=row, features=features_fn(symbol),
                current_price=price_fn(symbol), session=session, now=now,
                orders_allowed=orders_allowed, emergency=emergency)
            outcomes.append(outcome.as_dict())
            _record_exit_evaluation_outcome(conn, position_id, symbol, ok=True, now=now)
        except Exception as exc:  # noqa: BLE001
            logger.error("S6 exit evaluation failed for %s", symbol,
                         exc_info=True)
            outcomes.append(ExitOutcome(position_id, symbol, ACTION_BLOCKED,
                                        None, f"evaluation failed: {exc}"
                                        ).as_dict())
            _record_exit_evaluation_outcome(conn, position_id, symbol, ok=False, now=now)
    return outcomes


def retry_latched_exits(conn, *, broker_adapter, session=None, now=None,
                        orders_allowed=True) -> List[Dict[str, Any]]:
    """Re-submit exits latched when orders were not permitted.

    §7's requirement: a SELL that could not be sent is retried in the
    next execution window rather than waiting for the exit condition to
    re-trigger -- the condition already fired, and the position is
    already leaving.
    """
    if not orders_allowed:
        return []
    outcomes = []
    for position_id, row in position_store.load_live(conn):
        if row.get("status") != position_store.EXIT_PENDING:
            continue
        if row.get("exit_submitted"):
            continue
        reason = row.get("pending_exit_reason") or "SESSION_EXIT"
        try:
            outcome = _submit_sell_locked(
                conn, broker_adapter=broker_adapter, position_id=position_id,
                row=row, reason=reason, now=now)
            outcomes.append(outcome.as_dict())
        except Exception as exc:  # noqa: BLE001
            logger.error("S6 latched exit retry failed for %s", row["symbol"],
                         exc_info=True)
            outcomes.append({"position_id": position_id,
                             "symbol": row["symbol"],
                             "action": ACTION_BLOCKED, "detail": str(exc)})
    return outcomes


#: A SELL that is provably dead, on a position the broker still holds.
DEAD_SELL_RELEASED = "DEAD_SELL_RELEASED"
DEAD_SELL_QUANTITY_MISMATCH = "DEAD_SELL_QUANTITY_MISMATCH"


def recover_dead_exits(conn, *, broker, fills_for, positions=None,
                       now=None) -> List[Dict[str, Any]]:
    """Return a position whose SELL died to a state that can exit again.

    The gap this closes, measured on FLS on 2026-09-02:

        SELL accepted 19:59 -> filled ZERO -> gone from the book
        row EXIT_SUBMITTED, broker still holding 1 share, for hours
        `retry_latched_exits`  skips it: not EXIT_PENDING, already submitted
        `reconcile_unconfirmed_exits` skips it: the broker still holds it
        `sync_sell_fills` logs SELL_FILL_REPORTS_ZERO and continues
        reconciliation reports CLEAN, because nothing is inconsistent

    Nothing was wrong. Nothing was going to happen either.

    What must ALL hold before the latch is cleared:

      * the fill report is AUTHORITATIVE and TERMINAL -- past the
        publication window, so absence is finally evidence rather than
        latency (brokers/kis_fill_inquiry.NO_FILL_CONFIRMATION_GRACE_SECONDS)
      * cumulative filled is BELOW what the row still says it holds, so
        there is real remaining exposure
      * the broker independently confirms it still holds that remainder

    The last one is the one that makes oversell impossible: the quantity
    that may be retried is the broker's own number, and a row claiming
    more than the account holds is refused outright rather than reduced
    to fit. Disagreement is a reconciliation question, not something to
    paper over on the way to sending an order.

    Partial fills keep their economics. `sync_sell_fills` has already
    reduced the row to the unsold remainder and settled the intent for
    what did fill, so what is retried here is the remainder only -- the
    filled shares are never sold twice.
    """
    current = now or datetime.now(timezone.utc)
    outcomes: List[Dict[str, Any]] = []

    try:
        book = {}
        for p in (positions if positions is not None else broker.get_positions()) or ():
            symbol = str(getattr(p, "symbol", "") or "").upper()
            try:
                book[symbol] = int(getattr(p, "quantity", 0) or 0)
            except (TypeError, ValueError):
                book[symbol] = 0
    except Exception as exc:  # noqa: BLE001 - an unreadable broker is not
        # a confirmation of anything, and this releases nothing without one.
        logger.warning("S6 dead-exit recovery: broker unreadable: %s",
                       type(exc).__name__)
        return [{"status": "BROKER_UNREADABLE"}]

    for pid, row in position_store.load_live(conn):
        if row.get("status") != position_store.EXIT_SUBMITTED:
            continue
        if not row.get("exit_submitted"):
            continue
        symbol = str(row.get("symbol") or "").upper()
        held_row = int(row.get("quantity") or 0)

        try:
            fill = fills_for(row)
        except Exception:  # noqa: BLE001 - unreadable is not dead
            logger.warning("S6 %s: dead-exit fill lookup failed; releasing "
                           "nothing", symbol, exc_info=True)
            outcomes.append({"position_id": pid, "symbol": symbol,
                             "status": "FILL_LOOKUP_FAILED"})
            continue

        # None means the inquiry could not resolve -- UNKNOWN, or inside
        # the publication window. Neither is proof the order is dead.
        if not fill or not fill.get("terminal"):
            outcomes.append({"position_id": pid, "symbol": symbol,
                             "status": "SELL_NOT_TERMINAL"})
            continue

        sold = int(fill.get("filled_quantity") or 0)
        if sold >= held_row:
            # The ordinary path owns this: it has a fill that answers the
            # whole position and will close it with the real price.
            outcomes.append({"position_id": pid, "symbol": symbol,
                             "status": "FILL_AVAILABLE_NORMAL_PATH"})
            continue

        broker_qty = book.get(symbol, 0)
        if broker_qty < held_row:
            # The account holds less than the row claims. Retrying would
            # risk selling shares that are not there; retiring would claim
            # an execution nobody corroborated. Report and leave it.
            logger.warning(
                "S6 %s: %s -- row holds %s, broker holds %s; not releasing",
                symbol, DEAD_SELL_QUANTITY_MISMATCH, held_row, broker_qty)
            outcomes.append({"position_id": pid, "symbol": symbol,
                             "status": DEAD_SELL_QUANTITY_MISMATCH,
                             "row_quantity": held_row,
                             "broker_quantity": broker_qty})
            continue

        # The old order stays in the ledger exactly as the broker
        # reported it. Only the intent is ended, so the next attempt can
        # reserve its own.
        # No reduction happens here, and that is load-bearing. The
        # quantity guard immediately above returns unless
        # `broker_qty >= held_row`: reaching this line means the account
        # still holds everything the row claims, so no share of this
        # position has left it and there is nothing to take off. The
        # reduction that used to sit here subtracted the dead SELL's
        # filled quantity unconditionally, on top of the reduction
        # `sync_sell_fills` had already applied for the same fill -- a
        # position of 10 whose abandoned SELL filled 4 went to 6 and then
        # to 2, four shares removed twice, leaving the book BELOW the
        # account and inverting the gate mismatch the reduction exists to
        # prevent.
        #
        # The case where the row does exceed the broker -- the shares are
        # genuinely gone -- never arrives here; it is the
        # DEAD_SELL_QUANTITY_MISMATCH branch above, which deliberately
        # refuses to act without corroboration.
        _abort_intent(conn, pid)
        released = position_store.release_dead_exit(
            conn, pid, reason=row.get("exit_reason"), now=current)
        logger.warning(
            "S6 %s: %s -- SELL %s is terminal with %s of %s filled and the "
            "broker still holds %s; returned to EXIT_PENDING for retry",
            symbol, DEAD_SELL_RELEASED, fill.get("order_id"), sold,
            held_row, broker_qty)
        outcomes.append({
            "position_id": pid, "symbol": symbol,
            "status": DEAD_SELL_RELEASED, "released": bool(released),
            "dead_broker_order_id": fill.get("order_id"),
            "previously_filled": sold,
            "retryable_quantity": int(
                (position_store.load(conn, pid) or {}).get("quantity")
                or held_row),
            "exit_reason": row.get("exit_reason"),
        })
    return outcomes


def reconcile_unconfirmed_exits(conn, *, broker, positions=None,
                                open_orders=None, fills_for=None,
                                session=None, now=None) -> List[Dict[str, Any]]:
    """Retire an EXIT_SUBMITTED row the broker no longer backs.

    The case this exists for, from 2026-09-01:

      * MTCH sat EXIT_SUBMITTED qty 2 for five and a half hours
      * the broker held no MTCH and had no open order
      * our ledger said the sell settled FILLED 2.0 of 2.0
      * KIS's execution history contained no such sell -- while returning
        full detail for all ten other S6 sells in the same window
      * every S6 entry deferred behind the stale row

    So the position is provably gone and provably not exitable, but the
    execution this system believes it performed cannot be corroborated.
    That is a weaker claim than a fill, and it is recorded as the weaker
    claim: no exit price, no derived PnL, and a reason that says what is
    actually known.

    What must hold before anything is retired:

      * the broker reports no position in the symbol
      * the broker reports no open order in the symbol
      * a live fill inquiry does NOT report a usable fill -- if it does,
        the ordinary `sync_sell_fills` path owns this row and closes it
        with the real price
      * the row is EXIT_SUBMITTED with an exit already submitted

    Broker-flat alone is deliberately not sufficient anywhere here: it is
    the precondition, never the evidence. A position can vanish from a
    balance for reasons that are not a fill, which is the whole reason
    this does not claim one.
    """
    current = now or datetime.now(timezone.utc)
    outcomes: List[Dict[str, Any]] = []

    try:
        held = {str(getattr(p, "symbol", "") or "").upper()
                for p in (positions if positions is not None
                          else broker.get_positions())}
        resting = {str((o.get("symbol") if isinstance(o, dict)
                        else getattr(o, "symbol", "")) or "").upper()
                   for o in (open_orders if open_orders is not None
                             else broker.get_open_orders())}
    except Exception as exc:  # noqa: BLE001 - an unreadable broker is not
        # a flat one. Retiring on a failed read would be the fabrication
        # this function exists to avoid.
        logger.warning("broker unreadable; retiring nothing: %s", exc)
        return [{"status": "BROKER_UNREADABLE", "detail": str(exc)[:200]}]

    for pid, row in position_store.load_live(conn):
        symbol = str(row.get("symbol") or "").upper()
        if row.get("status") != position_store.EXIT_SUBMITTED:
            continue
        if not row.get("exit_submitted"):
            continue
        if symbol in held:
            outcomes.append({"position_id": pid, "symbol": symbol,
                             "status": "BROKER_STILL_HOLDS"})
            continue
        if symbol in resting:
            outcomes.append({"position_id": pid, "symbol": symbol,
                             "status": "BROKER_HAS_OPEN_ORDER"})
            continue

        # A usable fill means the ordinary path owns this row and will
        # close it with the price the trade actually ended at. Only the
        # absence of one gets the weaker terminal state.
        if fills_for is not None:
            try:
                fill = fills_for(row)
            except Exception:  # noqa: BLE001 - unreadable is not absent
                logger.warning("S6 %s: fill lookup failed; not retiring",
                               symbol, exc_info=True)
                outcomes.append({"position_id": pid, "symbol": symbol,
                                 "status": "FILL_LOOKUP_FAILED"})
                continue
            if fill and int(fill.get("filled_quantity") or 0) > 0:
                outcomes.append({"position_id": pid, "symbol": symbol,
                                 "status": "FILL_AVAILABLE_NORMAL_PATH"})
                continue
            # PUBLICATION GRACE, the SELL side of the BUY fix.
            #
            # `as_store_fill()` returns None while an order has neither
            # filled nor terminated -- which is exactly the window where
            # KIS has stopped listing an order and has not yet published
            # its executions. Reading that None as "no fill" retires a
            # position whose SELL is about to be confirmed.
            #
            # HBAN, 2026-09-02: SELL submitted 16:27:59, retired here at
            # 16:29:41 -- 101 seconds -- and the authoritative fill
            # appeared at 16:32:23. The trade was real and its exit price
            # was thrown away; the row still says PnL UNKNOWN.
            #
            # So absence is not resolution until the inquiry itself says
            # the order is finished. The grace lives in
            # `kis_fill_inquiry`, so BUY and SELL wait the same way.
            if not fill or not fill.get("terminal"):
                outcomes.append({"position_id": pid, "symbol": symbol,
                                 "status": "SELL_FILL_UNRESOLVED"})
                continue

        logger.warning(
            "S6 %s: %s -- broker holds none and has no open order, but this "
            "system cannot confirm its own SELL executed; retiring without "
            "an exit price",
            symbol, EXTERNALLY_CLOSED_SELL_UNCONFIRMED)
        closed = position_store.close_position(
            conn, pid, reason=EXTERNALLY_CLOSED_SELL_UNCONFIRMED,
            exit_price=None, exit_session=_session_name(session), now=current)
        # The intent is ABORTED, not CONFIRMED: confirming would assert the
        # very execution that could not be corroborated.
        _abort_intent(conn, pid)
        outcomes.append({
            "position_id": pid, "symbol": symbol,
            "status": EXTERNALLY_CLOSED_SELL_UNCONFIRMED,
            "closed": bool(closed),
            "previous_quantity": row.get("quantity"),
            "exit_price": None,
            "reconciled_at": current.isoformat(),
        })
    return outcomes


def filled_before_abort(intent) -> int:
    """How much an abandoned exit intent had already filled.

    Zero for a missing intent or an unreadable quantity -- an abandoned
    SELL whose progress cannot be read must not shrink the position.
    """
    if not intent:
        return 0
    try:
        return max(int(float(intent.get("confirmed_filled_qty") or 0)), 0)
    except (TypeError, ValueError):
        return 0


def _active_intent(conn, position_id):
    """The position's live exit intent, or None. Never raises: a ledger we
    cannot read must not stop a fill from being applied."""
    from state_store import exit_intent_ledger as eil

    try:
        return eil.get_active_intent(conn, position_id)
    except Exception:  # noqa: BLE001
        logger.warning("S6 could not read the exit intent for %s",
                       position_id, exc_info=True)
        return None


def apply_confirmed_exit_fill(conn, position_id, intent, cumulative_sold,
                              *, now=None) -> int:
    """Apply a confirmed SELL fill to `quantity` EXACTLY ONCE.

    The single owner of "this exit sold shares, take them off the
    position". Three paths used to do it independently -- the ordinary
    fill sync, dead-sell recovery and the stale-cancel release -- each
    subtracting the broker's CUMULATIVE filled quantity as though it were
    news. It is not news the second time. A position of 10 whose
    abandoned SELL filled 4 went to 6 in the fill sync and then to 2 in
    recovery: the same four shares removed twice, leaving the book below
    what the account actually held and inverting the very gate mismatch
    the reduction exists to prevent.

    `cumulative_sold` is the broker's running total for this exit, not an
    increment. What has already been taken off is the intent's
    `confirmed_filled_qty`, which is only ever written here alongside the
    reduction it describes -- so the difference is exactly the shares this
    observation adds, and a repeated observation yields zero.

    Modelled on positions.lifecycle._apply_exit_fill_progress, which has
    kept the S1 exit path free of this bug by keying off the same
    cumulative field. Returns the shares taken off by THIS call.
    """
    already = filled_before_abort(intent)
    try:
        target = max(int(float(cumulative_sold or 0)), 0)
    except (TypeError, ValueError):
        return 0

    if target < already:
        # A cumulative fill must never regress. Giving shares back on a
        # bad read would invent stock the account does not hold, so this
        # reports and changes nothing rather than guessing.
        logger.warning(
            "S6 %s: exit fill regressed from %s to %s -- position quantity "
            "left alone", position_id, already, target)
        return 0

    delta = target - already
    if delta <= 0:
        return 0

    before = position_store.load(conn, position_id) or {}
    position_store.reduce_after_partial_exit(conn, position_id, sold=delta,
                                             now=now)
    after = position_store.load(conn, position_id) or {}
    if int(after.get("quantity") or 0) == int(before.get("quantity") or 0):
        # reduce_after_partial_exit declined -- it refuses to take a
        # position below one share, leaving that to the close path. The
        # intent must not record the shares as applied when they were not,
        # or the next observation would skip them.
        return 0

    if intent:
        from state_store import exit_intent_ledger as eil

        try:
            eil.update_progress(conn, intent["intent_id"], target)
        except Exception:  # noqa: BLE001
            # The shares are off the position either way. Losing the
            # ledger's copy risks re-applying them later, so it is logged
            # loudly rather than swallowed.
            logger.warning(
                "S6 %s: quantity reduced by %s but the exit intent could not "
                "record it -- a later observation may re-apply the same fill",
                position_id, delta, exc_info=True)
    return delta


def _abort_intent(conn, position_id):
    """End the exit intent without claiming the fill was confirmed.

    `mark_confirmed` takes a confirmed_filled_qty and means exactly that.
    There is no confirmed quantity here, so the intent is aborted instead:
    a terminal state that does not assert an execution.
    """
    from state_store import exit_intent_ledger as eil

    try:
        intent = eil.get_active_intent(conn, position_id)
        if intent:
            eil.mark_aborted(conn, intent["intent_id"])
        # Returned so the caller can see how much this abandoned SELL had
        # already filled. `mark_aborted` deliberately asserts no
        # execution, but the ledger still carries the progress recorded
        # while the order was live, and that quantity is gone from the
        # account whether or not the order was ever confirmed.
        return intent
    except Exception:  # noqa: BLE001 - the position is already terminal;
        # losing the ledger's copy must not undo that.
        logger.warning("could not abort exit intent for %s", position_id,
                       exc_info=True)
        return None
