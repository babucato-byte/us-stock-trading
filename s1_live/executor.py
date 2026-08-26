"""The S1 live cycle the server runs on its own.

One tick does two things, in this order and for this reason:

    1. EXITS   evaluated first, and evaluated even when entry is blocked.
               An entry gate that also stopped liquidation would trap the
               account in the position the gate exists to escape, so the
               exit half of a tick shares no preconditions with the entry
               half beyond "the broker is reachable".
    2. ENTRY   only if every gate passes.

Nothing here re-implements a decision. Entry runs
`kis_live_trading.run_live_buy_entry_cycle()`, which owns the gates; exit
runs `s1_live/exit_runtime.py`, which owns S1_EXIT_V0. This module
supplies the three things neither of them can obtain for itself -- the
current session, a realtime price, and the daily trend structure -- and
records what happened.

What S1 takes from kis_position_manager, and what it does not
--------------------------------------------------------------
It takes FILL SYNCHRONISATION. It does not take the exit policy.

Those were conflated once, and it cost. `sync_kis_fills_and_manage_exits()`
does two separable things: it records what the broker actually filled
(bookkeeping every strategy needs) and it applies the scalping stop/target
/EOD policy (which S1 must never acquire -- a -8% stop on a position sized
against -6%, liquidated 60 minutes after entry regardless of trend).
Skipping the whole function skipped the bookkeeping too, so the first live
S1 fill left its ledger record stuck at ENTRY_SUBMITTED: `orders` and
`fills` stayed empty, reconciliation compared a real KIS holding against
internal=0, and every subsequent entry was blocked as a lost order.

So the fill half is called here, and the exit half refuses S1 positions at
the source (`EXIT_MANAGED_ELSEWHERE_STRATEGY_IDS`). S1 exits stay in
`s1_positions`, decided by `s1_live/exit_policy.py`.

ORDER_ACCEPTED is not FILLED
----------------------------
A submitted order creates no local position. `sync_fills()` reads the
broker's own position and uses ITS average price as the entry, because
every R level is measured from that number and an intended limit price
would put the stop wrong by the slippage.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STRATEGY_ID = "hma_early_trend"

STATUS_NO_CANDIDATE = "LIVE_ACTIVE_NO_CANDIDATE"
STATUS_NO_AFFORDABLE = "NO_AFFORDABLE_ELIGIBLE_CANDIDATE"
STATUS_SESSION_CLOSED = "SESSION_NOT_ORDERABLE"
STATUS_ENTRY_BLOCKED = "ENTRY_BLOCKED"
STATUS_SUBMITTED = "FIRST_S1_LIVE_ORDER_SUBMITTED"
STATUS_HELD = "ENTRY_LIMIT_REACHED"

#: Sessions in which the pilot may place an order. The rollout config
#: refuses `allow_extended_hours=True`, so this list is what that refusal
#: means in practice -- not a second, looser opinion about sessions.
ORDERABLE_MARKET_STATES = ("REGULAR", "OPEN")


@dataclass
class CycleReport:
    """§13: one structured record per tick, whatever happened."""

    started_at: str
    trading_day: Optional[str] = None
    market_state: Optional[str] = None
    session_orderable: bool = False
    exits: List[Dict[str, Any]] = field(default_factory=list)
    entry_status: Optional[str] = None
    entry_detail: Optional[str] = None
    submitted: List[str] = field(default_factory=list)
    blocked: List[Any] = field(default_factory=list)
    skipped: List[Any] = field(default_factory=list)
    positions_synced: List[Dict[str, Any]] = field(default_factory=list)
    ledger_sync: Dict[str, Any] = field(default_factory=dict)
    account: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return dict(vars(self))


def resolve_session(now=None):
    """The current session, and whether an order may be placed in it.

    An unrecognised or non-regular state is NOT orderable. That is the
    fail-closed direction: a session we cannot name is not one we know
    the broker accepts.
    """
    import market_hours

    from s1_live.exit_runtime import SessionPolicy

    try:
        state = market_hours.get_market_state(now) if now else market_hours.get_market_state()
    except Exception as exc:
        logger.warning("market state unavailable -- treating session as closed: %s", exc)
        return SessionPolicy("UNKNOWN", orders_allowed=False,
                             verification="MARKET_STATE_UNAVAILABLE")
    orderable = str(state).upper() in ORDERABLE_MARKET_STATES
    return SessionPolicy(
        str(state), orders_allowed=orderable,
        verification="VERIFIED" if orderable else "SESSION_NOT_ORDERABLE")


def make_price_fn(broker):
    """Realtime price for the exit policy, from KIS itself.

    Raises rather than returning a stale or invented number: the caller
    holds the position rather than acting on a price it cannot get.
    """
    from market_data.exchange_registry import build_kis_instrument

    def price_fn(symbol):
        # `build_kis_instrument` returns (instrument, exchange_record).
        # Passing the tuple through reaches KIS as an object with no
        # `.exchange`, which fails only once a position exists -- so it
        # would have surfaced on the first exit tick rather than in any
        # test that ran without one.
        instrument, _record = build_kis_instrument(symbol)
        price = broker.get_current_price(instrument)
        if price is None or not isinstance(price, (int, float)) or price <= 0:
            raise ValueError(f"no usable realtime price for {symbol}: {price!r}")
        return float(price)

    return price_fn


def make_features_fn(trading_day=None, provider=None):
    """Daily trend structure for the exit's trend axis.

    Built from bars through the PREVIOUS completed session, exactly as the
    entry signal is. The exit reads the realtime price separately; mixing
    a forming bar into HMA200 here would make the trend axis flicker
    intraday for the same reason it would make the entry flicker.

    Returns None for a symbol whose structure cannot be computed, which
    makes the trend axis abstain rather than guess.
    """
    from scanners.base.market_data_provider import default_provider
    from scanners.base import features as feature_builder
    from s1_live import same_day_scan as sds

    shared = provider or default_provider(cached=False)
    signal_day = sds.signal_day_for(trading_day)
    lookback = 0

    def features_fn(symbol):
        nonlocal lookback
        if not lookback:
            from scanners.base.features import minimum_daily_bars
            lookback = int(minimum_daily_bars() * 1.6) + 30
        data = shared.get_symbol_data(
            symbol, daily_lookback_days=lookback, intraday_interval="5m",
            intraday_lookback_days=0, want_premarket=False)
        bundle = sds._truncated_bundle(data, signal_day)
        return feature_builder.build_features(bundle, require_intraday=False)

    return features_fn


def sync_fills(conn, broker, *, trading_day, now=None) -> List[Dict[str, Any]]:
    """Turn CONFIRMED broker positions into S1 position state.

    Only positions the broker actually reports are recorded, and the entry
    price is the broker's own average fill. A submitted-but-unfilled order
    produces nothing here, which is the ORDER_ACCEPTED != FILLED rule
    expressed as code rather than as a comment.
    """
    from s1_live import position_store as ps

    recorded = []
    try:
        positions = broker.get_positions()
    except Exception as exc:
        logger.error("could not read broker positions -- no fills synced: %s", exc)
        return recorded

    known = ps.open_symbols(conn)
    for position in positions:
        symbol = str(getattr(position, "symbol", "")).upper()
        quantity = int(getattr(position, "quantity", 0) or 0)
        average = getattr(position, "average_fill_price", None)
        if not symbol or quantity < 1 or symbol in known:
            continue
        # `known` can only ever contain S1's OWN rows, so a position
        # another strategy opened looks unclaimed here. That is how S1
        # adopted S6's DT: one share, two owners, two exit engines each
        # believing they had to sell it.
        #
        # Ownership is asked of the ledger first (the order that produced
        # the fill was signed by a strategy) and of every per-strategy
        # book second. Anything ambiguous refuses -- an unattributed
        # position is visible and reportable, a doubly-owned one is not.
        from reconciliation import ownership

        permitted, why = ownership.may_adopt(conn, symbol,
                                             strategy_id=STRATEGY_ID)
        if not permitted:
            logger.warning(
                "S1 will not adopt the broker holding of %s: %s", symbol, why)
            continue
        if average is None or float(average) <= 0:
            logger.error("broker reports %s qty=%d with no usable average price "
                         "-- refusing to invent an entry price", symbol, quantity)
            continue
        try:
            position_id = ps.open_position(
                conn, symbol=symbol, strategy_id=STRATEGY_ID,
                signal_id=f"s1-fill-{symbol}-{trading_day}",
                entry_price=float(average), quantity=quantity, now=now)
        except ps.DuplicateS1PositionError:
            continue
        except ps.S1PositionStoreError as exc:
            logger.error("could not record S1 position for %s: %s", symbol, exc)
            continue
        recorded.append({"position_id": position_id, "symbol": symbol,
                         "quantity": quantity, "entry_price": float(average),
                         "source": "BROKER_CONFIRMED_FILL"})
        logger.info("S1 position recorded from confirmed fill: %s qty=%d @ %.4f",
                    symbol, quantity, float(average))
    return recorded


def run_exit_half(conn, *, broker, broker_adapter, session, trading_day, now=None):
    """Exits. Deliberately independent of every entry gate."""
    from s1_live import exit_runtime as er
    from s1_live import position_store as ps

    if ps.live_count(conn) == 0:
        return []
    price_fn = make_price_fn(broker)
    features_fn = make_features_fn(trading_day)
    outcomes = er.run_exit_cycle(
        conn, broker_adapter=broker_adapter, price_fn=price_fn, session=session,
        features_fn=features_fn, session_date=trading_day, now=now)
    return [o.as_dict() for o in outcomes]


def run_entry_half(conn, *, broker, session, now=None):
    """Entry. Refuses outright when the session cannot take an order."""
    import kis_live_trading as klt

    if not session.orders_allowed:
        return STATUS_SESSION_CLOSED, f"session {session.name} is not orderable", {}
    try:
        results = klt.run_live_buy_entry_cycle(broker=broker, now=now)
    except klt.KISLiveTradingError as exc:
        return STATUS_ENTRY_BLOCKED, str(exc), {}
    submitted = [s for s in (results.get("submitted") or [])]
    if submitted:
        return STATUS_SUBMITTED, None, results
    skipped = results.get("skipped") or []
    if skipped and all("BUDGET" in str(s).upper() or "cash" in str(s).lower()
                       for s in skipped):
        return STATUS_NO_AFFORDABLE, None, results
    if not skipped and not (results.get("blocked") or []):
        return STATUS_NO_CANDIDATE, None, results
    return STATUS_ENTRY_BLOCKED, None, results


def run_cycle(*, broker, broker_adapter=None, conn=None, now=None) -> CycleReport:
    """One full S1 tick: exits, fill sync, then entry."""
    from state_store import db as state_db

    stamp = now or datetime.now(timezone.utc)
    report = CycleReport(started_at=stamp.isoformat())
    owns_conn = conn is None
    if conn is None:
        conn = state_db.open_db()
    try:
        from scanners.base.trading_calendar import us_trading_day

        report.trading_day = us_trading_day()
        session = resolve_session()
        report.market_state = session.name
        report.session_orderable = session.orders_allowed

        if broker_adapter is None:
            from live_pilot import armed
            broker_adapter = armed.build_adapter(broker)

        # 1. EXITS FIRST -- before any entry gate is even consulted.
        try:
            report.exits = run_exit_half(
                conn, broker=broker, broker_adapter=broker_adapter,
                session=session, trading_day=report.trading_day, now=now)
        except Exception as exc:
            logger.error("S1 exit half failed this tick: %s", exc, exc_info=True)
            report.error = f"EXIT_HALF_FAILED: {exc}"

        # 2a. Record what the broker filled into the ORDER ledger, so the
        # position lifecycle advances past ENTRY_SUBMITTED and
        # reconciliation compares like with like. The exit half of that
        # function refuses S1 positions; only the bookkeeping applies.
        try:
            import kis_position_manager

            ledger = kis_position_manager.sync_kis_fills_and_manage_exits(
                kis_broker=broker, broker_adapter=broker_adapter, now=now, conn=conn)
            report.ledger_sync = {
                "synced_fills": list(ledger.get("synced_fills") or []),
                "reconciliation_blocked": list(ledger.get("reconciliation_blocked") or [])[:5],
                "skipped": list(ledger.get("skipped") or [])[:5],
            }
        except Exception as exc:
            # A ledger sync failure must not stop the exit half that has
            # already run, nor silently pass as success.
            logger.error("ledger fill sync failed this tick: %s", exc, exc_info=True)
            report.ledger_sync = {"error": str(exc)[:200]}

        # 2b. Confirmed broker positions become S1 position state.
        report.positions_synced = sync_fills(
            conn, broker, trading_day=report.trading_day, now=now)

        # 3. ENTRY.
        status, detail, results = run_entry_half(
            conn, broker=broker, session=session, now=now)
        report.entry_status, report.entry_detail = status, detail
        report.submitted = list(results.get("submitted") or [])
        report.blocked = list(results.get("blocked") or [])[:10]
        report.skipped = list(results.get("skipped") or [])[:10]

        # 4. A submitted order may have filled inside this same tick.
        if report.submitted:
            report.positions_synced += sync_fills(
                conn, broker, trading_day=report.trading_day, now=now)

        try:
            from s1_live import position_store as ps
            # `deposit_usd` is KIS's settled foreign-currency DEPOSIT
            # (frcr_dncl_amt_2). It is settlement-lagged: after buying TX
            # for $53.68 it still read 127.82, because an unsettled buy has
            # not left the deposit yet. Reported under a name that says so,
            # because "cash_usd" reads as "money available to spend" and
            # was misread that way once already.
            #
            # Order feasibility does NOT come from this number. Sizing uses
            # `get_orderable_usd(instrument, limit_price)` per symbol and
            # price (kis_live_trading.py), which is the figure KIS itself
            # will honour for an order.
            report.account = {
                "deposit_usd": broker.get_account_cash_usd(),
                "deposit_semantics": "SETTLED_DEPOSIT_NOT_SPENDABLE_BALANCE",
                "sizing_source": "get_orderable_usd_per_symbol_and_price",
                "broker_positions": len(broker.get_positions()),
                "open_orders": len(broker.get_open_orders()),
                "local_s1_positions": ps.live_count(conn),
            }
        except Exception as exc:
            report.account = {"error": str(exc)[:120]}
        return report
    finally:
        if owns_conn:
            conn.close()
