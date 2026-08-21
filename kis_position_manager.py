"""KIS position management -- the "existing 매도·손절·익절 정책을 KIS
실제 포지션 실행 경로에 연결" piece. This module does NOT define any new
sell rule. Every price-trigger decision (stop-loss, take-profit,
partial-exit, trailing/breakeven, time-stop, EOD forced close) is made
by `positions.lifecycle.check_and_manage()`, called here VERBATIM,
unmodified. This module's only two jobs are:

1. `create_kis_position_after_buy()` -- after a KIS buy order is
   ACCEPTED (kis_live_trading.py), create the position row
   (`positions.store.create_position()` + the ARMED/ENTRY_RESERVED/
   ENTRY_SUBMITTED transitions `positions.lifecycle.enter_position()`
   already performs for the Alpaca path, replicated here because this
   pipeline's candidate/signal source is the existing score-based
   `paper_strategy_order.analyze_stock()` -- not a `strategy/plugins/`
   Strategy object -- so `enter_position()` itself cannot be called
   directly; it requires a `strategy.generate_entry()` call this
   pipeline's entry signal was never built to produce). stop_price/
   target_1_price/target_2_price are deliberately left unset here and
   finalized in step 2, once KIS's REAL average fill price is known
   (spec: "entry_price = KIS 실제 평균체결가" -- not the signal/limit
   price).

2. `sync_kis_fills_and_manage_exits()` -- the periodic tick: KIS
   position/fill read -> reconciliation against the internal positions
   table (block, never auto-correct, on mismatch) -> apply new fills via
   `positions.lifecycle.record_fill()` (verbatim) -> on first full fill,
   finalize stop/target from the ACTUAL average_fill_price using the
   SAME formula already used elsewhere in this codebase for a live-pilot
   stop (`risk_config.STOP_LOSS_RATE`, reused verbatim by
   `live_readiness/live_entry_pipeline.py` for the analogous Alpaca-KRW
   pilot) and the SAME R-multiple target formula
   `strategy/plugins/vwap_micro_pullback_v1.calculate_targets()` already
   implements (inlined here as the identical formula, not reinvented,
   since instantiating that plugin class purely to call one stateless
   method would be a heavier and more fragile dependency than copying
   its two-line arithmetic verbatim) -> `positions.lifecycle.
   check_and_manage()` (verbatim) for every exit-eligible position.

SCOPE NOTE (documented, not silently omitted): `positions.lifecycle.
check_invalidation()` -- the strategy-invalidation exit reason -- is
NOT called here. It requires a `strategy` object with an `.invalidate()`
method; this pipeline's score-based entries have no such object, and
fabricating one would be exactly the "새로운 매도 전략을 추가" this
task was explicitly told not to do. `check_and_manage()`'s other five
exit reasons (STOP_LOSS, TARGET_2 full exit, TARGET_1 partial exit,
TIME_STOP, EOD_FORCED_CLOSE) are all reused, unmodified.
"""

import logging
from datetime import datetime, timedelta, timezone

import risk_config
from config import scalping_strategy_v1_config as strat_cfg

logger = logging.getLogger(__name__)
from config.live_exit_flags import LiveExitFlags
from domain.position import Position
from execution import idempotency, order_repository
from positions import lifecycle, states, store
from reconciliation import reconciliation_state
from reconciliation.order_reconciler import reconcile_unknown_order
from reconciliation.position_reconciler import reconcile_positions
from state_store import db as state_db
from operations import live_notifications


class KISPositionManagerError(Exception):
    """Raised for a structural failure (e.g. KIS reads failing outright).
    Per-position failures are recorded on that position or simply skip
    that position for this tick -- they never raise, so one bad symbol
    can't stop the whole sync cycle from servicing every other position."""


def create_kis_position_after_buy(*, strategy_id, strategy_version, symbol, quantity,
                                   client_order_id, broker_order_id, now=None):
    """Mirrors positions.lifecycle.enter_position()'s SETUP_DETECTED ->
    ARMED -> ENTRY_RESERVED -> ENTRY_SUBMITTED transitions exactly (same
    state names, same states.validate_transition() calls) -- only the
    entry-signal source differs (already-submitted KIS order, not a
    strategy.generate_entry() call). stop_price/target_1_price/
    target_2_price stay None until finalize_stop_and_targets_from_fill()
    runs. Returns the created position record."""
    current = now or datetime.now(timezone.utc)
    record = store.create_position(
        strategy_id, strategy_version, symbol, client_order_id=client_order_id,
        requested_qty=quantity,
    )
    position_id = record["position_id"]
    with store.locked_position(position_id) as locked:
        states.validate_transition(locked["state"], states.ARMED)
        locked["state"] = states.ARMED
        locked["state_history"].append(
            {"state": states.ARMED, "at": current.isoformat(), "reason": "KIS buy order accepted"}
        )
        states.validate_transition(locked["state"], states.ENTRY_RESERVED)
        locked["state"] = states.ENTRY_RESERVED
        locked["state_history"].append(
            {"state": states.ENTRY_RESERVED, "at": current.isoformat(), "reason": "KIS order id known"}
        )
        states.validate_transition(locked["state"], states.ENTRY_SUBMITTED)
        locked["state"] = states.ENTRY_SUBMITTED
        locked["broker_order_id"] = broker_order_id
        locked["entry_time"] = current.isoformat()
        locked["state_history"].append(
            {"state": states.ENTRY_SUBMITTED, "at": current.isoformat(),
             "reason": f"KIS broker_order_id={broker_order_id}"}
        )
    return store.load_position(position_id)


def finalize_stop_and_targets_from_fill(position_id, average_fill_price):
    """Called once a position reaches STOP_ACTIVE (i.e. record_fill()
    just fully filled it). Computes stop_price from risk_config.
    STOP_LOSS_RATE and target_1_price/target_2_price from the same
    R-multiple formula strategy/plugins/vwap_micro_pullback_v1.
    calculate_targets() implements -- both existing, already-deployed-
    elsewhere constants/formulas, applied here to KIS's REAL average
    fill price rather than the signal/limit price (spec requirement)."""
    stop_price = average_fill_price * (1 + risk_config.STOP_LOSS_RATE)
    risk_per_share = average_fill_price - stop_price
    target_1_price = average_fill_price + risk_per_share * strat_cfg.TARGET_1_R_MULTIPLE
    target_2_price = average_fill_price + risk_per_share * strat_cfg.TARGET_2_R_MULTIPLE
    with store.locked_position(position_id) as locked:
        if locked["state"] != states.STOP_ACTIVE or locked["stop_price"] is not None:
            # Already finalized (idempotent re-entry) or the position has
            # moved on since -- never clobber a later state's data.
            return store.load_position(position_id)
        locked["stop_price"] = stop_price
        locked["target_1_price"] = target_1_price
        locked["target_2_price"] = target_2_price
    return store.load_position(position_id)


_EXIT_ELIGIBLE_STATES = (states.STOP_ACTIVE, states.TARGET_1_ACTIVE, states.PARTIAL_EXITED, states.TRAILING)

#: Strategies whose exits are owned elsewhere. Their fills are still
#: SYNCHRONISED here -- recording what the broker did is bookkeeping every
#: strategy needs, and skipping it is what left the first S1 position
#: stuck at ENTRY_SUBMITTED with reconciliation reporting internal=0
#: against a real KIS holding.
#:
#: What is skipped is the EXIT half below: the scalping stop/target/EOD
#: policy. An S1 position sized against a -6% stop must not acquire the
#: -8% one, and must not be liquidated 60 minutes after entry. S1 exits
#: live in s1_positions and are decided by s1_live/exit_policy.py.
EXIT_MANAGED_ELSEWHERE_STRATEGY_IDS = frozenset({
    "S1_HMA_EARLY_TREND_V1",
    # S2 joins for the same reason S1 is here: its exit is owned by
    # S2_EXIT_V0, and a position evaluated by two exit policies gets two
    # SELLs for one holding. This guard covers EXIT ownership only --
    # fill synchronisation is deliberately NOT gated by it, because that
    # conflation is exactly what cost S1 its bookkeeping once already.
    "S2_VOLUME_ACCUMULATION_V1",
})
_FILL_PENDING_STATES = (states.ENTRY_SUBMITTED, states.PARTIALLY_FILLED)


def _as_datetime(value):
    """A stored ISO timestamp as an aware datetime, or None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _find_kis_fill_for_order(kis_broker, broker_order_id, *, now, since=None):
    """CODEX-045: `ft_ccld_qty` fill rows are per-execution-event, not
    cumulative -- a buy order filled across two separate KIS fill rows
    (1 share then 1 share) must sum to 2, not report the first row's
    qty alone. positions.lifecycle.record_fill() explicitly requires
    the *cumulative* filled quantity (CODEX-027), so this returns the
    sum across every matching fill row and the resulting weighted
    average price, never a single event's price.

    `since` is the moment the ORDER was placed. Without it the lookup
    spans only today, so a fill from a previous session can never be
    found: an order submitted yesterday and not synchronised that day
    stays at ENTRY_SUBMITTED permanently, and reconciliation keeps
    reporting a real holding as internal=0. That is not hypothetical --
    it is what happened to the first live S1 fill.

    The window is the order's own age, not an arbitrary lookback, and it
    starts a day EARLIER than the order timestamp because that timestamp
    is UTC while KIS dates rows by its own trading day; an order placed
    late in the ET session already belongs to the previous UTC date. Over-
    fetching is harmless because rows are matched on the exact order
    number.
    """
    if not broker_order_id:
        return None
    start = now
    if since is not None:
        try:
            start = min(since, now)
        except TypeError:
            start = now
    start_date = (start - timedelta(days=1)).strftime("%Y%m%d")
    try:
        fills = kis_broker.get_fills(start_date=start_date, end_date=now.strftime("%Y%m%d"))
    except Exception:
        return None
    cumulative_qty = 0.0
    weighted_price_sum = 0.0
    for fill in fills:
        if fill.get("ODNO") != broker_order_id and fill.get("odno") != broker_order_id:
            continue
        try:
            event_qty = float(fill.get("ft_ccld_qty") or fill.get("FT_CCLD_QTY") or 0)
            event_price = float(fill.get("ft_ccld_unpr3") or fill.get("FT_CCLD_UNPR3") or 0)
        except (TypeError, ValueError):
            continue
        if event_qty <= 0 or event_price <= 0:
            continue
        cumulative_qty += event_qty
        weighted_price_sum += event_qty * event_price
    if cumulative_qty <= 0:
        return None
    return {"filled_qty": cumulative_qty, "average_fill_price": weighted_price_sum / cumulative_qty}


def _reconcile_account_and_orders(*, kis_broker, conn, open_positions, kis_positions, now):
    """CODEX-044: the account-wide half of reconciliation that the Order
    Gate's `reconciliation_ok`/`has_unknown_order` checks actually depend
    on. Runs every tick so kis_live_trading.py's buy path and
    kis_broker_adapter.py's sell path always have a recent
    (reconciliation_state.DEFAULT_MAX_AGE_SECONDS-fresh) result to read --
    never the previous `reconciliation_ok=True` constant. Never raises:
    a KIS read failure here must leave the PREVIOUS (or absent) recorded
    result in place, which is exactly what should make the gates fail
    closed rather than silently pass.

    CODEX-044 (ordering): NOTHING is recorded until every required KIS
    read has already succeeded. The previous version computed the
    position comparison, recorded `clean=...` immediately, and only then
    queried open orders/fills -- so a failure of that second query still
    left a freshly-stamped clean timestamp behind, i.e. a failed read
    could refresh the very record the gates use to decide the account is
    reconciled. The recorded result now also accounts for any order
    still sitting in UNKNOWN after this tick's resolution attempt."""
    try:
        kis_open_orders = kis_broker.get_open_orders()
        kis_fills = kis_broker.get_fills(start_date=now.strftime("%Y%m%d"), end_date=now.strftime("%Y%m%d"))
    except Exception:
        # Can't complete a real reconciliation this tick. Record NOTHING:
        # the previous (or absent) result simply ages out, which is the
        # fail-closed outcome -- never an exception that would abort the
        # tick, and never a refreshed clean timestamp.
        return

    for row in idempotency.list_unknown_orders(conn):
        outcome = reconcile_unknown_order(
            row["internal_order_id"], row["broker_order_id"], kis_open_orders, kis_fills,
            requested_quantity=row["requested_quantity"],
        )
        if outcome.resolved:
            order_repository.compare_and_set_state(
                conn, order_id=row["internal_order_id"], expected_state="UNKNOWN",
                next_state=outcome.confirmed_status, event_type="UNKNOWN_RECONCILED",
                event_payload={"reason": outcome.reason},
                expected_version=row["version"], via_reconciliation=True,
            )

    internal_positions = [
        Position(
            symbol=record["symbol"], quantity=record["remaining_qty"],
            average_fill_price=record["average_fill_price"] or 0.0,
            unrealized_pnl=0.0, realized_pnl=0.0, as_of=now, source="internal_store",
        )
        for record in open_positions.values() if record["remaining_qty"]
    ]
    mismatches = reconcile_positions(internal_positions, kis_positions)

    # Strategy attribution and coverage, alongside the broker comparison.
    #
    # The comparison above asks "does the account store agree with the
    # broker". It structurally CANNOT ask "did every strategy's position
    # reach the account store" -- if fill synchronisation skipped one,
    # the account still agrees with the broker and the strategy simply
    # holds something nobody counts. That is how S1 lost its bookkeeping
    # once, and it is what `coverage_gaps` looks for.
    #
    # Deliberately NOT summed into internal_positions: the strategy
    # tables are bookkeeping layered on the account record, not a second
    # copy of it. TX is in `positions` and in `s1_positions`; adding
    # them would report 2 against the broker's 1 and fail-close every
    # entry, including the strategy that is trading correctly.
    try:
        from reconciliation import internal_holdings

        holdings_summary = internal_holdings.summary(
            conn, [{"symbol": r["symbol"], "venue": r.get("venue"),
                    "quantity": r["remaining_qty"]}
                   for r in open_positions.values() if r["remaining_qty"]])
        for line in holdings_summary["attribution"]:
            logger.info("reconciliation strategy holdings -- %s", line)
        for gap in holdings_summary["coverage_gaps"]:
            if gap["gap"] == internal_holdings.GAP_NOT_IN_ACCOUNT:
                logger.error(
                    "reconciliation coverage gap: %s holds %s/%s x%s but the "
                    "account store has x%s -- fill sync may have skipped it",
                    gap["strategy_id"], gap["symbol"], gap["venue"],
                    gap["strategy_quantity"], gap["account_quantity"])
    except Exception:  # noqa: BLE001 - a diagnostic must never be able to
        # fail the reconciliation it is describing.
        logger.warning("could not compute strategy holdings attribution",
                       exc_info=True)
        holdings_summary = None
    still_unknown = idempotency.has_unknown_order(conn)
    unknown_count = idempotency.count_unknown_orders(conn)
    try:
        from operations import kill_switch

        halted = kill_switch.is_halted()
    except Exception:                                 # noqa: BLE001
        # kill_switch fails closed to "halted"; an unreadable state must
        # be recorded as halted, never as clear.
        halted = True
    reconciliation_state.record_result(
        clean=not mismatches and not still_unknown,
        mismatch_count=len(mismatches) + (1 if still_unknown else 0),
        unknown_count=unknown_count, halt=halted, now=now,
    )


def _notify_fill_delta(record, *, symbol, previously_filled):
    """PARTIAL_FILL / FILL_COMPLETED, once per genuine fill delta.

    The entry path polls KIS repeatedly, and `lifecycle.record_fill()` is
    idempotent for a repeated observation of the same cumulative fill.
    Notifying on every poll would turn a 2-then-3 fill into a stream of
    identical messages, so this compares the cumulative quantity across
    the call and stays silent when nothing moved.

    Never raises: `notify()` cannot, and everything else here is
    attribute reads with defaults.
    """
    filled = record.get("filled_qty") or 0
    if filled <= (previously_filled or 0):
        return  # the same fill observed again
    requested = record.get("requested_qty") or 0
    price = record.get("average_fill_price")
    if requested and filled >= requested:
        live_notifications.notify(
            live_notifications.FILL_COMPLETED,
            live_notifications.fill_completed_fields(
                symbol=symbol, filled_qty=filled, fill_price=price,
                position_qty=record.get("remaining_qty"), average_cost=price),
        )
    else:
        live_notifications.notify(
            live_notifications.PARTIAL_FILL,
            live_notifications.partial_fill_fields(
                symbol=symbol, filled_qty=filled,
                remaining_qty=max(0, requested - filled), average_fill_price=price),
        )


def sync_kis_fills_and_manage_exits(*, kis_broker, broker_adapter, now=None, conn=None):
    """One tick of the sell/exit monitoring cycle. Never raises for a
    single-position failure -- returns a summary dict instead so a
    scheduler can log/alert without the whole cycle aborting."""
    current = now or datetime.now(timezone.utc)
    summary = {"synced_fills": [], "managed": [], "reconciliation_blocked": [], "skipped": []}

    try:
        kis_positions = kis_broker.get_positions()
    except Exception as exc:
        raise KISPositionManagerError(f"KIS position read failed, aborting this tick: {exc}") from exc

    kis_qty_by_symbol = {p.symbol: p.quantity for p in kis_positions}
    open_positions = store.load_non_terminal()
    exit_flags = LiveExitFlags.from_env()

    owns_conn = conn is None
    conn = conn or state_db.open_db()
    try:
        _reconcile_account_and_orders(
            kis_broker=kis_broker, conn=conn, open_positions=open_positions,
            kis_positions=kis_positions, now=current,
        )
    finally:
        if owns_conn:
            conn.close()

    for position_id, record in open_positions.items():
        symbol = record["symbol"]

        if record["state"] in (states.PARTIAL_EXIT_SUBMITTED, states.EXIT_SUBMITTED):
            # A prior exit (partial or full) was submitted to KIS but not
            # yet confirmed filled -- reconcile against KIS's own order/
            # fill history (via broker_adapter.get_order_by_client_
            # order_id(), reused verbatim, never a new order attempt).
            try:
                record = lifecycle.reconcile_pending_exit(position_id, broker=broker_adapter) or record
            except Exception as exc:
                summary["skipped"].append((symbol, f"reconcile_pending_exit failed: {exc}"))
                continue

        if record["state"] in _FILL_PENDING_STATES:
            fill = _find_kis_fill_for_order(
                kis_broker, record.get("broker_order_id"), now=current,
                since=_as_datetime(record.get("entry_time")))
            if fill is not None:
                # Captured BEFORE the call so the notification below can
                # tell a genuine new fill from the same cumulative fill
                # observed again on the next poll. record_fill() is
                # idempotent for a repeat observation, so comparing the
                # cumulative quantity across it is the dedupe -- no new
                # durable state is introduced for this.
                previously_filled = record.get("filled_qty") or 0
                try:
                    updated = lifecycle.record_fill(
                        position_id, fill["filled_qty"], fill["average_fill_price"],
                    )
                except Exception as exc:
                    summary["skipped"].append((symbol, f"record_fill failed: {exc}"))
                    continue
                summary["synced_fills"].append(symbol)
                _notify_fill_delta(updated, symbol=symbol, previously_filled=previously_filled)
                if updated["state"] == states.STOP_ACTIVE:
                    record = finalize_stop_and_targets_from_fill(position_id, fill["average_fill_price"])
                else:
                    record = updated
            else:
                summary["skipped"].append((symbol, "no KIS fill yet"))
                continue

        if record.get("strategy_id") in EXIT_MANAGED_ELSEWHERE_STRATEGY_IDS:
            # Its fill has just been recorded above, which is the point of
            # letting it through this loop at all. The exit policy below
            # belongs to another owner.
            summary["skipped"].append(
                (symbol, f"exit managed elsewhere ({record.get('strategy_id')})"))
            continue

        if record["state"] not in _EXIT_ELIGIBLE_STATES:
            continue

        # Reconciliation gate (spec §16/§3): internal remaining_qty must
        # match KIS's actual reported quantity for this symbol before any
        # exit management runs for it -- never auto-correct, never
        # auto-sell to "fix" a mismatch.
        kis_qty = kis_qty_by_symbol.get(symbol, 0)
        if kis_qty != record["remaining_qty"]:
            summary["reconciliation_blocked"].append(
                (symbol, f"internal remaining_qty={record['remaining_qty']!r} != KIS quantity={kis_qty!r}")
            )
            # Only on a real divergence -- a matching position sends
            # nothing. No account number: symbol and quantities only.
            live_notifications.notify(
                live_notifications.POSITION_MISMATCH,
                {"symbol": symbol, "kis_qty": kis_qty,
                 "local_qty": record["remaining_qty"],
                 "reconciliation_state": "MISMATCH",
                 "action": "NEW_ENTRY_BLOCKED"},
            )
            continue

        if record["stop_price"] is None:
            # Defensive: should be unreachable (finalize_stop_and_targets_
            # from_fill() runs synchronously above the instant STOP_ACTIVE
            # is reached), but check_and_manage() requires a real
            # stop_price to compare against -- never call it with None.
            summary["skipped"].append((symbol, "stop_price not yet finalized"))
            continue

        try:
            current_price = broker_adapter.kis_broker.get_current_price(broker_adapter._instrument(symbol))
        except Exception as exc:
            summary["skipped"].append((symbol, f"KIS price read failed: {exc}"))
            continue

        try:
            lifecycle.check_and_manage(
                position_id, current_price=current_price, broker=broker_adapter, now=current,
                order_date=current.date().isoformat(),
                enable_partial_profit=exit_flags.enable_partial_profit,
                enable_trailing_stop=exit_flags.enable_trailing_stop,
                enable_time_stop=exit_flags.enable_time_stop,
                enable_eod_exit=exit_flags.enable_eod_exit,
            )
            summary["managed"].append(symbol)
        except Exception as exc:
            summary["skipped"].append((symbol, f"check_and_manage failed: {exc}"))

    return summary
