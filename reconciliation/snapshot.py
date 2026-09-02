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

TCN-02A: the snapshot now also carries WHICH symbols each disagreement
is about, and the broker's own per-symbol quantities it observed. The
booleans above are still the decision for a BUY. The per-symbol facts
exist so the SELL gate can ask a narrower question -- "is anything
ambiguous about the position I am about to reduce?" -- through
`execution/sell_safe_evidence.py`, instead of refusing every exit on
the account because of a disagreement about some other symbol. A local
read failing is `ReconciliationLocalStateError`, distinct from a broker
read failing, because the two are different operational states.
"""

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)

from reconciliation import fill_window

DEFAULT_MAX_AGE_SECONDS = 30


class ReconciliationUnavailableError(Exception):
    """Raised by build_snapshot() when a required KIS read (positions,
    open orders, fills) fails. There is deliberately no "assume clean"
    fallback: with no snapshot, the caller has nothing to hand the gate,
    so the order is blocked before any transport call."""


class ReconciliationLocalStateError(ReconciliationUnavailableError):
    """TCN-02A: the INTERNAL side could not be read -- the order ledger,
    a position book, the exit-intent table. A subclass of the
    unavailable error so every existing caller still fails closed
    identically; a distinct type so a policy can tell "the broker did
    not answer" from "our own database did not answer"."""

    reason_code = "LOCAL_STATE_FAILURE"


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
    # TCN-02A: per-symbol facts, all defaulting to "not carried" so a
    # snapshot built by hand (tests, older callers) behaves exactly as
    # before -- and, for the sell-evidence path, fails closed: a
    # snapshot that did not record the broker's quantities cannot
    # confirm one.
    #
    # `kis_position_quantities` is a tuple of (SYMBOL, qty) pairs rather
    # than a dict so the dataclass stays frozen and hashable.
    kis_position_quantities: Optional[Tuple[Tuple[str, int], ...]] = None
    #: Symbols whose internal and KIS quantities disagree, or that two
    #: strategies both claim.
    position_mismatch_symbols: FrozenSet[str] = frozenset()
    #: Symbols implicated in an open-order or fill disagreement: an
    #: untracked KIS open order, an internally-live order KIS has no
    #: record of, a fill for a dead order, an over-fill.
    order_dirty_symbols: FrozenSet[str] = frozenset()
    #: Symbols with an order still in UNKNOWN.
    unknown_order_symbols: FrozenSet[str] = frozenset()

    def is_clean(self) -> bool:
        return bool(
            self.positions_match and self.open_orders_match and self.fills_match
            and not self.has_unknown_orders
        )

    def mismatch_count(self) -> int:
        return len(self.detail)

    def confirmed_broker_quantity(self, symbol) -> Optional[int]:
        """The quantity KIS reported for `symbol` when this snapshot was
        built. None when the snapshot did not carry quantities at all
        (nothing was confirmed); 0 when it did and the symbol was
        absent (the broker positively reported no position)."""
        if self.kis_position_quantities is None:
            return None
        name = str(symbol or "").upper()
        for held_symbol, quantity in self.kis_position_quantities:
            if held_symbol == name:
                return int(quantity)
        return 0


# Idempotency statuses that mean "this codebase believes the order is
# live at KIS right now" -- KIS must therefore either still list it as
# open or show fills for it.
_INTERNAL_LIVE_STATUSES = ("SUBMITTING", "ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING")
# Statuses that mean "this order never reached, or no longer exists at,
# the market" -- a KIS fill for one of these is a genuine contradiction.
_INTERNAL_DEAD_STATUSES = ("REJECTED", "CANCELLED")

_SYMBOL_KEYS = ("pdno", "PDNO", "ovrs_pdno", "OVRS_PDNO", "symbol")


def _order_id_of(row):
    if isinstance(row, dict):
        return row.get("ODNO") or row.get("odno")
    return None


def _symbol_of(row):
    """The symbol a KIS order/fill row is about, upper-cased, or ""."""
    if isinstance(row, dict):
        for key in _SYMBOL_KEYS:
            value = row.get(key)
            if value:
                return str(value).upper()
    return ""


def _check_open_orders(conn, kis_open_orders, kis_fills):
    """Both directions matter. A KIS open order this codebase has never
    heard of means something (a manual order, another process, a lost
    response) is trading this account behind our back. An internally-live
    order that appears in NEITHER KIS's open-order list NOR its fill
    history means we believe an order is working that KIS has no record
    of. Either way, new orders must not be approved.

    Returns (ok, detail, internal_live_ids, dirty_symbols)."""
    from execution import idempotency

    detail = []
    dirty_symbols = set()
    kis_open_by_id = {}
    for order in kis_open_orders:
        order_id = _order_id_of(order)
        if order_id:
            kis_open_by_id[order_id] = _symbol_of(order)
    kis_open_ids = set(kis_open_by_id)
    kis_fill_ids = {oid for oid in (_order_id_of(f) for f in kis_fills) if oid}
    internal_ids = set()
    internal_symbol_by_id = {}
    for row in idempotency.list_orders_by_status(conn, _INTERNAL_LIVE_STATUSES):
        broker_order_id = row["broker_order_id"]
        if not broker_order_id:
            # SUBMITTING with no broker id yet -- the response has not
            # been read, so there is nothing to compare. Its ambiguity is
            # covered by the UNKNOWN check, not here.
            continue
        internal_ids.add(broker_order_id)
        internal_symbol_by_id[broker_order_id] = str(row["symbol"] or "").upper()
    for order_id in sorted(kis_open_ids - internal_ids):
        detail.append(f"KIS reports open order {order_id!r} that is not tracked internally")
        if kis_open_by_id.get(order_id):
            dirty_symbols.add(kis_open_by_id[order_id])
    for order_id in sorted(internal_ids - kis_open_ids - kis_fill_ids):
        detail.append(
            f"order {order_id!r} is recorded internally as live but KIS reports neither an open "
            "order nor any fill for it"
        )
        if internal_symbol_by_id.get(order_id):
            dirty_symbols.add(internal_symbol_by_id[order_id])
    return (not detail), detail, internal_ids, dirty_symbols


def _check_fills(conn, kis_fills, internal_live_ids):
    """A fill row for an order we recorded as REJECTED/CANCELLED, or a
    cumulative filled quantity larger than what we requested, is a
    contradiction between KIS's truth and ours.

    Returns (ok, detail, dirty_symbols)."""
    from execution import idempotency

    detail = []
    dirty_symbols = set()
    cumulative = {}
    fill_symbol_by_id = {}
    for fill in kis_fills:
        order_id = _order_id_of(fill)
        if not order_id:
            continue
        if _symbol_of(fill):
            fill_symbol_by_id.setdefault(order_id, _symbol_of(fill))
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
    internal_symbol_by_id = {}
    for row in idempotency.list_orders_with_broker_id(conn):
        internal_symbol_by_id[row["broker_order_id"]] = str(row["symbol"] or "").upper()
        if row["requested_quantity"] is not None:
            requested[row["broker_order_id"]] = float(row["requested_quantity"])

    def _implicate(order_id):
        name = internal_symbol_by_id.get(order_id) or fill_symbol_by_id.get(order_id)
        if name:
            dirty_symbols.add(name)

    for order_id, filled in sorted(cumulative.items()):
        if order_id in dead_ids:
            detail.append(
                f"KIS reports fills for order {order_id!r} that is recorded internally as "
                f"{dead_ids[order_id]}"
            )
            _implicate(order_id)
        expected = requested.get(order_id)
        if expected is not None and filled > expected:
            detail.append(
                f"KIS cumulative filled quantity {filled!r} for order {order_id!r} exceeds the "
                f"internally requested quantity {expected!r}"
            )
            _implicate(order_id)
    del internal_live_ids  # kept for signature symmetry/readability only
    return (not detail), detail, dirty_symbols


def build_snapshot(*, broker, conn, account_id, symbol=None, now=None,
                    internal_positions=None, source="execution_engine"):
    """Collects the REAL state -- KIS balance/positions, KIS open orders,
    KIS fills, internal positions, internal open orders, internal
    UNKNOWN orders -- and returns the judgement as an immutable snapshot.

    Raises ReconciliationUnavailableError if ANY of the three KIS reads
    fails, and ReconciliationLocalStateError (a subclass) if the internal
    side cannot be read. It never returns a "degraded" snapshot: a
    snapshot exists only when every fact behind it was actually
    observed."""
    from execution import idempotency
    from execution.order_repository import FatalRepositoryConnectionError
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

    # TCN-02A: everything below reads OUR side. A failure here is a
    # different operational state from a broker that did not answer,
    # and is named as such -- while still, as a subclass, failing every
    # existing caller closed in exactly the same way.
    try:
        if internal_positions is None:
            internal_positions = load_internal_positions(now=current, conn=conn)

        detail = []
        position_mismatches = reconcile_positions(internal_positions, kis_positions)
        for mismatch in position_mismatches:
            detail.append(
                f"position mismatch for {mismatch.symbol}: internal={mismatch.internal_quantity!r} "
                f"KIS={mismatch.kis_quantity!r} ({mismatch.reason})"
            )
        mismatch_symbols = {str(m.symbol or "").upper() for m in position_mismatches}
        # A symbol claimed by two strategies is a position disagreement even
        # when the TOTAL agrees with the broker -- especially then, because
        # matching totals are what make a double owner invisible. Folded into
        # `positions_match` so it blocks orders exactly as any other position
        # mismatch does.
        conflict_pairs = _ownership_conflict_pairs(conn)
        conflict_detail = [_format_conflict(symbol, owners) for symbol, owners in conflict_pairs]
        detail.extend(conflict_detail)
        mismatch_symbols.update(str(symbol or "").upper() for symbol, _ in conflict_pairs)

        positions_match = not position_mismatches and not conflict_detail

        open_orders_match, open_order_detail, internal_live_ids, open_order_dirty = \
            _check_open_orders(conn, kis_open_orders, kis_fills)
        detail.extend(open_order_detail)

        fills_match, fill_detail, fill_dirty = _check_fills(conn, kis_fills, internal_live_ids)
        detail.extend(fill_detail)

        unknown_rows = idempotency.list_unknown_orders(conn)
        for row in unknown_rows:
            detail.append(
                f"order {row['internal_order_id']!r} ({row['symbol']}/{row['side']}) is still UNKNOWN"
            )
        unknown_symbols = {str(row["symbol"] or "").upper() for row in unknown_rows}
    except FatalRepositoryConnectionError:
        raise  # CODEX-059: a poisoned connection is not a "local read failed"
    except ReconciliationUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - the internal side failed to read
        raise ReconciliationLocalStateError(
            f"internal state read failed: {type(exc).__name__}: {exc}") from exc

    quantities = tuple(sorted(
        (str(p.symbol).upper(), int(p.quantity)) for p in kis_positions))

    return ReconciliationSnapshot(
        account_id=account_id or "", symbol=symbol, checked_at=current,
        positions_match=positions_match, open_orders_match=open_orders_match,
        fills_match=fills_match, has_unknown_orders=bool(unknown_rows), source=source,
        detail=tuple(detail),
        kis_position_quantities=quantities,
        position_mismatch_symbols=frozenset(s for s in mismatch_symbols if s),
        order_dirty_symbols=frozenset(s for s in (open_order_dirty | fill_dirty) if s),
        unknown_order_symbols=frozenset(s for s in unknown_symbols if s),
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

    # Per-strategy books, deduplicated by symbol against the general
    # store: S1 writes to BOTH, and counting it twice would invent shares
    # the account does not hold -- a mismatch pointing the other way.
    #
    # Deduplication here is only ever between the two books of ONE
    # strategy. It is deliberately NOT used to collapse a symbol claimed
    # by two DIFFERENT strategies: that arithmetic would produce a total
    # matching the broker and a reconciliation that reads PASS, while
    # hiding the one condition that lets two exit engines sell the same
    # share. `ownership_conflicts` reports those separately, and the
    # caller fails closed on them.
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


def _format_conflict(symbol, owners):
    from reconciliation import ownership

    return f"{ownership.OWNERSHIP_CONFLICT}: {symbol} is claimed by {', '.join(owners)}"


def _ownership_conflict_pairs(conn):
    """(symbol, owners) for every symbol claimed by more than one
    strategy. Empty when `conn` is None or the check cannot run."""
    if conn is None:
        return []
    try:
        from reconciliation import ownership

        return list(ownership.conflicts(conn))
    except Exception:  # noqa: BLE001
        logger.warning("ownership conflicts could not be evaluated",
                       exc_info=True)
        return []


def ownership_conflicts(conn):
    """Symbols claimed by more than one strategy, as mismatch detail.

    A conflict is a reconciliation failure in its own right, independent
    of whether the totals happen to agree with the broker. One share
    owned twice means two exit engines each believe they must sell it,
    and the totals matching is exactly what makes that invisible.
    """
    return [_format_conflict(symbol, owners)
            for symbol, owners in _ownership_conflict_pairs(conn)]


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
