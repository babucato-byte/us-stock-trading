"""CODEX-044: the immutable, *self-collected* reconciliation snapshot the
Order Gate requires before any live order may be approved.

Before this module, the gate contexts carried two raw booleans
(`reconciliation_ok`, `has_unknown_order`) supplied by whatever caller
happened to build the context. A boolean is not evidence: nothing in the
gate could tell "a real internal-vs-KIS comparison ran 200ms ago and was
clean" apart from "some caller passed True". This module replaces that
with a frozen snapshot that can ONLY be produced by actually querying
KIS and the internal ledger (`build_snapshot()`), and a verifier
(`verify_snapshot()`) the gate runs on every order -- buy and sell
alike -- which fails closed on every axis:

    no snapshot                       -> blocked
    snapshot for another account      -> blocked
    snapshot for another symbol       -> blocked
    checked_at older than the TTL     -> blocked
    checked_at in the future          -> blocked
    any KIS read failed               -> build_snapshot() raises; no snapshot exists to approve with
    internal/KIS positions disagree   -> blocked
    internal/KIS open orders disagree -> blocked
    internal/KIS fills contradict     -> blocked
    ANY order still in UNKNOWN        -> blocked

`RECONCILIATION_MAX_AGE_SECONDS` (default 30s -- deliberately much
tighter than reconciliation_state.DEFAULT_MAX_AGE_SECONDS' 300s
periodic-tick tolerance, since this snapshot is built fresh inside the
order path itself, not read from a periodic record) is the TTL. It is
enforced even though `build_snapshot()` normally runs milliseconds
before `verify_snapshot()`: the TTL is what makes a snapshot that was
somehow captured earlier (a longer pipeline, a retry, a restart)
unusable rather than silently accepted.

UNKNOWN is deliberately checked ACCOUNT-WIDE, not per (symbol, side):
an unresolved ambiguous order anywhere on the account means this
codebase does not know the account's true exposure, which blocks every
new order regardless of which symbol or side it is for.
"""

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

from reconciliation import fill_window

DEFAULT_MAX_AGE_SECONDS = 30


class ReconciliationUnavailableError(Exception):
    """Raised by build_snapshot() when a required KIS read (positions,
    open orders, fills) fails. There is deliberately no "assume clean"
    fallback: with no snapshot, the caller has nothing to hand the gate,
    so the order is blocked before any transport call."""


class ReconciliationBlockedError(Exception):
    """Raised by verify_snapshot() for a missing, mismatched, stale or
    dirty snapshot."""


def max_age_seconds():
    """Env-overridable TTL. An unparseable/non-positive override falls
    back to the conservative default rather than disabling the check."""
    raw = os.environ.get("RECONCILIATION_MAX_AGE_SECONDS")
    if raw is None:
        return DEFAULT_MAX_AGE_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_MAX_AGE_SECONDS
    return value


@dataclass(frozen=True)
class ReconciliationSnapshot:
    account_id: str
    symbol: Optional[str]
    checked_at: datetime
    positions_match: bool
    open_orders_match: bool
    fills_match: bool
    has_unknown_orders: bool
    source: str
    # Human-readable detail for the audit trail. Never used for a
    # decision -- the booleans above are the decision.
    detail: Tuple[str, ...] = ()

    def is_clean(self) -> bool:
        return bool(
            self.positions_match and self.open_orders_match and self.fills_match
            and not self.has_unknown_orders
        )

    def mismatch_count(self) -> int:
        return len(self.detail)


# Idempotency statuses that mean "this codebase believes the order is
# live at KIS right now" -- KIS must therefore either still list it as
# open or show fills for it.
_INTERNAL_LIVE_STATUSES = ("SUBMITTING", "ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING")
# Statuses that mean "this order never reached, or no longer exists at,
# the market" -- a KIS fill for one of these is a genuine contradiction.
_INTERNAL_DEAD_STATUSES = ("REJECTED", "CANCELLED")


def _order_id_of(row):
    if isinstance(row, dict):
        return row.get("ODNO") or row.get("odno")
    return None


def _check_open_orders(conn, kis_open_orders, kis_fills):
    """Both directions matter. A KIS open order this codebase has never
    heard of means something (a manual order, another process, a lost
    response) is trading this account behind our back. An internally-live
    order that appears in NEITHER KIS's open-order list NOR its fill
    history means we believe an order is working that KIS has no record
    of. Either way, new orders must not be approved."""
    from execution import idempotency

    detail = []
    kis_open_ids = {oid for oid in (_order_id_of(o) for o in kis_open_orders) if oid}
    kis_fill_ids = {oid for oid in (_order_id_of(f) for f in kis_fills) if oid}
    internal_ids = set()
    for row in idempotency.list_orders_by_status(conn, _INTERNAL_LIVE_STATUSES):
        broker_order_id = row["broker_order_id"]
        if not broker_order_id:
            # SUBMITTING with no broker id yet -- the response has not
            # been read, so there is nothing to compare. Its ambiguity is
            # covered by the UNKNOWN check, not here.
            continue
        internal_ids.add(broker_order_id)
    for order_id in sorted(kis_open_ids - internal_ids):
        detail.append(f"KIS reports open order {order_id!r} that is not tracked internally")
    for order_id in sorted(internal_ids - kis_open_ids - kis_fill_ids):
        detail.append(
            f"order {order_id!r} is recorded internally as live but KIS reports neither an open "
            "order nor any fill for it"
        )
    return (not detail), detail, internal_ids


def _check_fills(conn, kis_fills, internal_live_ids):
    """A fill row for an order we recorded as REJECTED/CANCELLED, or a
    cumulative filled quantity larger than what we requested, is a
    contradiction between KIS's truth and ours."""
    from execution import idempotency

    detail = []
    cumulative = {}
    for fill in kis_fills:
        order_id = _order_id_of(fill)
        if not order_id:
            continue
        raw_qty = fill.get("ft_ccld_qty") or fill.get("FT_CCLD_QTY") or 0
        try:
            qty = float(raw_qty)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        cumulative[order_id] = cumulative.get(order_id, 0.0) + qty

    dead_ids = {}
    for row in idempotency.list_orders_by_status(conn, _INTERNAL_DEAD_STATUSES):
        if row["broker_order_id"]:
            dead_ids[row["broker_order_id"]] = row["status"]
    requested = {}
    for row in idempotency.list_orders_with_broker_id(conn):
        if row["requested_quantity"] is not None:
            requested[row["broker_order_id"]] = float(row["requested_quantity"])

    for order_id, filled in sorted(cumulative.items()):
        if order_id in dead_ids:
            detail.append(
                f"KIS reports fills for order {order_id!r} that is recorded internally as "
                f"{dead_ids[order_id]}"
            )
        expected = requested.get(order_id)
        if expected is not None and filled > expected:
            detail.append(
                f"KIS cumulative filled quantity {filled!r} for order {order_id!r} exceeds the "
                f"internally requested quantity {expected!r}"
            )
    del internal_live_ids  # kept for signature symmetry/readability only
    return (not detail), detail


def build_snapshot(*, broker, conn, account_id, symbol=None, now=None,
                    internal_positions=None, source="execution_engine"):
    """Collects the REAL state -- KIS balance/positions, KIS open orders,
    KIS fills, internal positions, internal open orders, internal
    UNKNOWN orders -- and returns the judgement as an immutable snapshot.

    Raises ReconciliationUnavailableError if ANY of the three KIS reads
    fails. It never returns a "degraded" snapshot: a snapshot exists only
    when every fact behind it was actually observed."""
    from execution import idempotency
    from reconciliation.position_reconciler import reconcile_positions

    current = now or datetime.now(timezone.utc)

    def _unavailable(stage, exc):
        """HIGH-2: keep the specific reason. A rate-limited read must be
        legible as KIS_RATE_LIMIT, not as a generic unavailability -- the
        operator response is different (wait) from a real outage."""
        error = ReconciliationUnavailableError(f"{stage}: {exc}")
        error.reason_code = getattr(exc, "reason_code", None) or "KIS_UNAVAILABLE"
        return error

    try:
        kis_positions = broker.get_positions()
    except Exception as exc:
        raise _unavailable("KIS position read failed", exc) from exc
    try:
        kis_open_orders = broker.get_open_orders()
    except Exception as exc:
        raise _unavailable("KIS open-order read failed", exc) from exc
    try:
        # Not today-only. `_check_open_orders` below reports an
        # internally-live order that appears in neither KIS's open
        # orders nor its fills, and an order that filled on a PREVIOUS
        # session can never appear in today's fill list -- so a
        # today-only window made that mismatch permanent and blocked
        # every BUY for every strategy from the next morning onward.
        # The window is derived from the oldest order still believed
        # live (reconciliation/fill_window.py).
        kis_fills = fill_window.read_fills(broker, conn, now=current)
    except Exception as exc:
        raise _unavailable("KIS fill-history read failed", exc) from exc

    if internal_positions is None:
        internal_positions = load_internal_positions(now=current, conn=conn)

    detail = []
    position_mismatches = reconcile_positions(internal_positions, kis_positions)
    for mismatch in position_mismatches:
        detail.append(
            f"position mismatch for {mismatch.symbol}: internal={mismatch.internal_quantity!r} "
            f"KIS={mismatch.kis_quantity!r} ({mismatch.reason})"
        )
    positions_match = not position_mismatches

    open_orders_match, open_order_detail, internal_live_ids = _check_open_orders(
        conn, kis_open_orders, kis_fills,
    )
    detail.extend(open_order_detail)

    fills_match, fill_detail = _check_fills(conn, kis_fills, internal_live_ids)
    detail.extend(fill_detail)

    unknown_rows = idempotency.list_unknown_orders(conn)
    for row in unknown_rows:
        detail.append(
            f"order {row['internal_order_id']!r} ({row['symbol']}/{row['side']}) is still UNKNOWN"
        )

    return ReconciliationSnapshot(
        account_id=account_id or "", symbol=symbol, checked_at=current,
        positions_match=positions_match, open_orders_match=open_orders_match,
        fills_match=fills_match, has_unknown_orders=bool(unknown_rows), source=source,
        detail=tuple(detail),
    )


def load_internal_positions(*, now=None, conn=None):
    """The internal side of the position comparison.

    Reads BOTH the general lifecycle store (positions/store.py) and the
    per-strategy stores, because they are different books and a strategy
    lives in exactly one of them.

    This used to read only the general store, which was correct while
    every live position was written there. It stopped being correct when
    S6 began recording into `s6_positions` and nowhere else -- the right
    call, since `strategy_registry.POSITION_TABLES` maps S6 there and
    writing to both would put two exit engines on one position. But the
    reconciler was never told, so a filled S6 position read as
    "exists at KIS but not tracked internally", and the mismatch BLOCKED
    ITS OWN EXIT: the position could be opened and then not sold.

    A position invisible here is worse than one that is merely
    unattributed. Unattributed costs capacity; invisible costs the
    ability to leave.

    `conn` is optional so callers that already hold the state connection
    do not open a second one; without it the per-strategy side is skipped
    rather than guessed at.
    """
    from domain.position import Position
    from positions import store

    current = now or datetime.now(timezone.utc)
    internal = []
    seen = set()
    for record in store.load_non_terminal().values():
        remaining = record["remaining_qty"]
        if not remaining:
            continue
        seen.add(str(record["symbol"]).upper())
        internal.append(Position(
            symbol=record["symbol"], quantity=remaining,
            average_fill_price=record["average_fill_price"] or 0.0,
            unrealized_pnl=0.0, realized_pnl=0.0, as_of=current, source="internal_store",
        ))

    if conn is None:
        return internal

    # Per-strategy books. Deduplicated by symbol against the general
    # store: S1 writes to both, and counting it twice would invent shares
    # the account does not hold -- a mismatch in the other direction.
    try:
        from reconciliation import internal_holdings

        for _strategy, rows in (internal_holdings.strategy_holdings(conn) or {}).items():
            for symbol, _venue, quantity in rows or ():
                name = str(symbol or "").upper()
                if not name or not quantity or name in seen:
                    continue
                seen.add(name)
                internal.append(Position(
                    symbol=name, quantity=int(quantity),
                    average_fill_price=0.0, unrealized_pnl=0.0,
                    realized_pnl=0.0, as_of=current,
                    source="strategy_store",
                ))
    except Exception:  # noqa: BLE001 - a strategy book that cannot be
        # read must not turn a healthy reconciliation into a mismatch;
        # it is logged by `strategy_holdings` itself.
        logger.warning("per-strategy holdings unreadable; the position "
                       "comparison used the general store only",
                       exc_info=True)
    return internal


def verify_snapshot(snapshot, *, account_id, symbol=None, now=None, max_age=None):
    """Raises ReconciliationBlockedError unless `snapshot` is a genuine,
    current, clean ReconciliationSnapshot for exactly this account and
    symbol. Returns None on success (nothing to unpack, nothing a caller
    could mistake for a permissive default)."""
    if not isinstance(snapshot, ReconciliationSnapshot):
        raise ReconciliationBlockedError(
            "no ReconciliationSnapshot supplied -- new orders are blocked until a real "
            "internal-vs-KIS reconciliation has been performed"
        )
    if (snapshot.account_id or "") != (account_id or ""):
        raise ReconciliationBlockedError(
            "reconciliation snapshot was taken for a different account than this order"
        )
    if symbol is not None and snapshot.symbol is not None and snapshot.symbol != symbol:
        raise ReconciliationBlockedError(
            f"reconciliation snapshot is for {snapshot.symbol!r}, not {symbol!r}"
        )
    current = now or datetime.now(timezone.utc)
    checked_at = snapshot.checked_at
    if checked_at is None:
        raise ReconciliationBlockedError("reconciliation snapshot has no checked_at timestamp")
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current - checked_at).total_seconds()
    limit = max_age if max_age is not None else max_age_seconds()
    if age < 0:
        raise ReconciliationBlockedError(
            "reconciliation snapshot is timestamped in the future -- refusing to trust it"
        )
    if age > limit:
        raise ReconciliationBlockedError(
            f"reconciliation snapshot is {age:.1f}s old, exceeding the {limit}s limit -- "
            "a fresh reconciliation must run before any new order"
        )
    if not snapshot.positions_match:
        raise ReconciliationBlockedError(
            f"internal positions do not match KIS: {'; '.join(snapshot.detail) or 'mismatch'}"
        )
    if not snapshot.open_orders_match:
        raise ReconciliationBlockedError(
            f"internal open orders do not match KIS: {'; '.join(snapshot.detail) or 'mismatch'}"
        )
    if not snapshot.fills_match:
        raise ReconciliationBlockedError(
            f"internal order records contradict KIS fills: {'; '.join(snapshot.detail) or 'mismatch'}"
        )
    if snapshot.has_unknown_orders:
        raise ReconciliationBlockedError(
            "an UNKNOWN-state order exists on this account -- every new order is blocked until it "
            "is reconciled"
        )
