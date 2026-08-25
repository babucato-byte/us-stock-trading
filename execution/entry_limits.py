"""The authoritative state behind LIVE_ROLLOUT_MAX_POSITIONS and
LIVE_ROLLOUT_MAX_DAILY_ENTRIES.

The defect this closes
----------------------
Both limits were read and type-validated by
`config/live_rollout_config.py` and then consumed by nothing. A search of
the KIS entry path found `max_open_positions` and `max_daily_entries`
referenced in that config file and nowhere else: not in
`evaluate_buy_gate`, not in `kis_live_trading`, not in the shadow
evaluation. An operator setting `LIVE_ROLLOUT_MAX_POSITIONS=1` was
reading a number that restricted nothing.

Where the counts come from
--------------------------
Two durable, independent sources, never caller input:

  open positions  -- KIS's own `get_positions()`, filtered to quantity > 0.
                     The broker's view, not this codebase's belief about it.
  entry attempts  -- the `kis_order_idempotency` ledger, which
                     `execution/execution_engine.py` writes BEFORE any
                     network call, inside `single_run_lock()`. That
                     ordering is what makes it a reservation: a crash
                     between the row and the transport leaves the row
                     behind, so the slot stays consumed.

Why both, for the position cap
------------------------------
Counting only settled positions leaves a race: with max_positions=1 and
zero positions held, two candidates evaluated back to back would each
read "0 open" and each be approved, because the first one's order has not
filled yet. The cap is therefore computed over the UNION of symbols that
either hold a position or have an entry attempt still in flight -- a
union, so a symbol that is both (its order filled and became a position)
is one slot, not two.

Which attempts consume a daily slot
-----------------------------------
Every buy attempt for the day EXCEPT one that was definitively rejected
before the broker ever saw it (status REJECTED with no broker_order_id).
In particular UNKNOWN counts, permanently, until reconciled: an ambiguous
submission may well be a real order, and the one thing a daily cap must
never do is let a retry or an unresolved outcome buy a second slot.

A pre-transport rejection releasing its slot is the single exception, and
it is deliberate: nothing reached the broker, so nothing was entered. If
that ever needs to change, it is one predicate here, not a rule spread
across two pipelines.

Fail-closed
-----------
Every read that cannot be completed raises `EntryLimitStateUnavailable`.
There is no `except Exception: count = 0` anywhere in this module -- a
count of zero is the single most dangerous wrong answer a limit checker
can give, because it reads exactly like "nothing is open yet".
"""

from dataclasses import dataclass, field
from typing import FrozenSet, Mapping

from config import strategy_registry
from execution.order_repository import FatalRepositoryConnectionError
from market_hours import us_trading_day

# Reason codes. Distinct from the ordinary "the limit says no" codes,
# because the operator response differs: a limit block is the system
# working, an unavailable state is the system unable to tell.
POSITION_LIMIT_STATE_UNKNOWN = "POSITION_LIMIT_STATE_UNKNOWN"
DAILY_ENTRY_STATE_UNKNOWN = "DAILY_ENTRY_STATE_UNKNOWN"

MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
MAX_DAILY_ENTRIES = "MAX_DAILY_ENTRIES"
#: The candidate's own strategy is already using its slot, even though
#: the account has room. Distinct from MAX_OPEN_POSITIONS on purpose:
#: "the account is full" and "S6 already holds one" are different facts
#: and lead to different operator actions.
MAX_STRATEGY_POSITIONS = "MAX_STRATEGY_POSITIONS"
#: Something is in flight or held that cannot be attributed to a
#: strategy, so no per-strategy cap can be enforced honestly.
STRATEGY_ATTRIBUTION_UNKNOWN = "STRATEGY_ATTRIBUTION_UNKNOWN"

# An entry attempt stops holding a slot only when the broker definitively
# never saw it. Anything that reached (or may have reached) the wire keeps
# its slot -- including UNKNOWN.
_PRE_TRANSPORT_REJECTION = "REJECTED"


class EntryLimitStateUnavailable(Exception):
    """The limit state could not be established. Callers must block the
    entry -- never fall back to a count."""

    def __init__(self, message, *, reason_code):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class EntryLimitState:
    """What the gate needs to enforce both caps, already collected.

    Pure data: the gate performs no I/O, exactly as it performs none for
    the reconciliation snapshot it is handed.
    """

    max_open_positions: int
    max_daily_entries: int
    open_position_symbols: FrozenSet[str]
    pending_entry_symbols: FrozenSet[str]
    daily_entry_count: int
    trading_day: str
    #: Per-strategy slot cap, and the symbols each slot is using.
    #: Defaulted so every existing constructor call keeps working; a
    #: context built without them enforces the global cap exactly as
    #: before and reports the per-strategy one as unenforceable.
    max_positions_per_strategy: int = 1
    strategy_symbols: Mapping[str, FrozenSet[str]] = field(
        default_factory=dict)
    #: Symbols held or in flight that no strategy could be named for.
    #: Never empty-by-assumption: see `strategy_effective_count`.
    unattributed_symbols: FrozenSet[str] = frozenset()

    @property
    def open_position_count(self):
        return len(self.open_position_symbols)

    @property
    def pending_entry_count(self):
        return len(self.pending_entry_symbols)

    @property
    def effective_position_count(self):
        """Held plus in-flight, deduplicated by symbol -- an order that
        has already become a position must not be counted twice."""
        return len(self.open_position_symbols | self.pending_entry_symbols)

    def strategy_symbols_for(self, slot) -> FrozenSet[str]:
        """What `slot` is holding or has in flight, including anything
        unattributed.

        Unattributed symbols are added to EVERY slot rather than to
        none. A symbol nobody can claim might be this strategy's, and a
        cap that resolves that doubt in favour of trading is not a cap.
        In the normal case the set is empty and this is the plain count.
        """
        own = frozenset(self.strategy_symbols.get(slot, frozenset()))
        return own | frozenset(self.unattributed_symbols)

    def strategy_effective_count(self, slot) -> int:
        return len(self.strategy_symbols_for(slot))

    def as_audit_payload(self):
        """Operator-facing observability. Symbols and counts only: no
        account number, no order id, no price."""
        return {
            "max_open_positions": self.max_open_positions,
            "open_positions": self.open_position_count,
            "pending_entries": self.pending_entry_count,
            "effective_positions": self.effective_position_count,
            "max_daily_entries": self.max_daily_entries,
            "daily_entries": self.daily_entry_count,
            "trading_day": self.trading_day,
            "max_positions_per_strategy": self.max_positions_per_strategy,
            "strategy_positions": {
                slot: self.strategy_effective_count(slot)
                for slot in strategy_registry.LIVE_SLOTS
            },
            "unattributed": sorted(self.unattributed_symbols),
        }


def _open_position_symbols(broker):
    try:
        positions = broker.get_positions()
    except Exception as exc:  # noqa: BLE001 -- any read failure is unknown, not zero
        raise EntryLimitStateUnavailable(
            f"KIS position read failed: {type(exc).__name__}",
            reason_code=POSITION_LIMIT_STATE_UNKNOWN,
        ) from exc
    symbols = set()
    for position in positions or []:
        symbol = getattr(position, "symbol", None)
        quantity = getattr(position, "quantity", None)
        if not symbol:
            raise EntryLimitStateUnavailable(
                "a KIS position row carries no symbol; the open-position count "
                "cannot be established",
                reason_code=POSITION_LIMIT_STATE_UNKNOWN,
            )
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
            raise EntryLimitStateUnavailable(
                f"KIS position {symbol} has a non-numeric quantity",
                reason_code=POSITION_LIMIT_STATE_UNKNOWN,
            )
        if quantity > 0:
            symbols.add(str(symbol).upper())
    return frozenset(symbols)


def _pending_entry_symbols(conn, *, exclude_internal_order_id):
    """Buy attempts that are neither finished nor definitively unsent."""
    from execution.order_state_machine import TERMINAL_STATES

    try:
        rows = conn.execute(
            "SELECT internal_order_id, symbol, status, broker_order_id "
            "FROM kis_order_idempotency WHERE side = 'buy'"
        ).fetchall()
    except FatalRepositoryConnectionError:
        # Outranks "state unknown". A fatal connection fault has already
        # set HALT and must fail-stop the process so the OS releases the
        # SQLite lock; degrading it to a soft limit block would leave a
        # broken process running.
        raise
    except Exception as exc:  # noqa: BLE001
        raise EntryLimitStateUnavailable(
            f"durable order state is unreadable: {type(exc).__name__}",
            reason_code=POSITION_LIMIT_STATE_UNKNOWN,
        ) from exc

    symbols = set()
    for row in rows:
        if exclude_internal_order_id and row["internal_order_id"] == exclude_internal_order_id:
            continue
        status = row["status"]
        if status in TERMINAL_STATES and status != _PRE_TRANSPORT_REJECTION:
            # CANCELLED / FILLED: finished. A FILLED buy shows up as a
            # position instead, which the union already counts.
            continue
        if _never_reached_the_broker(row):
            continue
        symbols.add(str(row["symbol"]).upper())
    return frozenset(symbols)


def _never_reached_the_broker(row):
    return row["status"] == _PRE_TRANSPORT_REJECTION and not row["broker_order_id"]


def _held_symbols_by_slot(conn):
    """What each strategy's own position store says it is holding.

    The broker is the authority on WHETHER a position exists -- that is
    `_open_position_symbols` above and it is what the global cap counts.
    It cannot be the authority on WHOSE it is: KIS returns a symbol and
    a quantity, not a strategy. Attribution can only come from the store
    that opened the position, so that is what is read here.

    A store that cannot be read raises. A slot silently counted as empty
    is the same dangerous wrong answer as a zero position count, and for
    the same reason.
    """
    from config import strategy_registry as registry

    held = {}
    for slot, table in registry.POSITION_TABLES.items():
        placeholders = ",".join("?" * len(registry.HOLDING_STATUSES))
        statuses = sorted(registry.HOLDING_STATUSES)
        try:
            rows = conn.execute(
                f"SELECT symbol FROM {table} WHERE status IN ({placeholders})",
                statuses,
            ).fetchall()
        except FatalRepositoryConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EntryLimitStateUnavailable(
                f"{table} is unreadable, so {slot}'s slot usage cannot be "
                f"established: {type(exc).__name__}",
                reason_code=POSITION_LIMIT_STATE_UNKNOWN,
            ) from exc
        held[slot] = frozenset(
            str(row["symbol"]).upper() for row in rows if row["symbol"])
    return held


def _pending_symbols_by_slot(conn, *, exclude_internal_order_id):
    """In-flight entry attempts, grouped by the strategy that made them.

    Returns `(by_slot, unattributed)`. A row whose `strategy_id` is NULL
    or unrecognised goes into `unattributed` rather than being dropped:
    rows written before migration 18 have no strategy, and inventing one
    for them -- or ignoring them -- would free capacity that is really
    in use.
    """
    from config import strategy_registry as registry
    from execution.order_state_machine import TERMINAL_STATES

    try:
        rows = conn.execute(
            "SELECT internal_order_id, symbol, status, broker_order_id, strategy_id "
            "FROM kis_order_idempotency WHERE side = 'buy'"
        ).fetchall()
    except FatalRepositoryConnectionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EntryLimitStateUnavailable(
            f"durable order state is unreadable: {type(exc).__name__}",
            reason_code=POSITION_LIMIT_STATE_UNKNOWN,
        ) from exc

    by_slot = {slot: set() for slot in registry.LIVE_SLOTS}
    unattributed = set()
    for row in rows:
        if exclude_internal_order_id and row["internal_order_id"] == exclude_internal_order_id:
            continue
        status = row["status"]
        if status in TERMINAL_STATES and status != _PRE_TRANSPORT_REJECTION:
            continue
        if _never_reached_the_broker(row):
            continue
        symbol = str(row["symbol"]).upper()
        slot = registry.slot_for(row["strategy_id"])
        if slot is None:
            unattributed.add(symbol)
        else:
            by_slot.setdefault(slot, set()).add(symbol)
    return ({slot: frozenset(symbols) for slot, symbols in by_slot.items()},
            frozenset(unattributed))


def _daily_entry_count(conn, *, trading_day, exclude_internal_order_id):
    try:
        rows = conn.execute(
            "SELECT internal_order_id, status, broker_order_id FROM kis_order_idempotency "
            "WHERE side = 'buy' AND trading_date = ?",
            (trading_day,),
        ).fetchall()
    except FatalRepositoryConnectionError:
        raise  # see _pending_entry_symbols: fatal outranks "unknown"
    except Exception as exc:  # noqa: BLE001
        raise EntryLimitStateUnavailable(
            f"today's entry count is unreadable: {type(exc).__name__}",
            reason_code=DAILY_ENTRY_STATE_UNKNOWN,
        ) from exc
    count = 0
    for row in rows:
        if exclude_internal_order_id and row["internal_order_id"] == exclude_internal_order_id:
            continue
        if _never_reached_the_broker(row):
            continue
        count += 1
    return count


def collect(*, broker, conn, rollout, now=None, exclude_internal_order_id=None):
    """Gather the authoritative limit state, or raise.

    `exclude_internal_order_id` is the attempt being evaluated right now.
    The live path registers its idempotency row BEFORE the gate runs, so
    without this the very first candidate of the day would count itself
    and block itself. Shadow evaluation registers nothing and passes None.
    """
    try:
        trading_day = us_trading_day(now)
    except Exception as exc:  # noqa: BLE001
        raise EntryLimitStateUnavailable(
            f"the US trading day could not be determined: {type(exc).__name__}",
            reason_code=DAILY_ENTRY_STATE_UNKNOWN,
        ) from exc

    max_positions = getattr(rollout, "max_open_positions", None)
    max_entries = getattr(rollout, "max_daily_entries", None)
    for name, value in (("max_open_positions", max_positions),
                        ("max_daily_entries", max_entries)):
        # bool is an int subclass; True must not read as a limit of 1.
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise EntryLimitStateUnavailable(
                f"rollout {name} is not a positive int: {value!r}",
                reason_code=(POSITION_LIMIT_STATE_UNKNOWN if "position" in name
                             else DAILY_ENTRY_STATE_UNKNOWN),
            )

    max_per_strategy = getattr(rollout, "max_positions_per_strategy", 1)
    if isinstance(max_per_strategy, bool) or not isinstance(max_per_strategy, int) \
            or max_per_strategy < 1:
        raise EntryLimitStateUnavailable(
            f"rollout max_positions_per_strategy is not a positive int: "
            f"{max_per_strategy!r}",
            reason_code=POSITION_LIMIT_STATE_UNKNOWN,
        )

    open_symbols = _open_position_symbols(broker)
    pending_symbols = _pending_entry_symbols(
        conn, exclude_internal_order_id=exclude_internal_order_id)
    held_by_slot = _held_symbols_by_slot(conn)
    pending_by_slot, unattributed_pending = _pending_symbols_by_slot(
        conn, exclude_internal_order_id=exclude_internal_order_id)

    strategy_symbols = {
        slot: frozenset(held_by_slot.get(slot, frozenset()))
        | frozenset(pending_by_slot.get(slot, frozenset()))
        for slot in strategy_registry.LIVE_SLOTS
    }

    # A position the broker reports that no strategy store claims. It
    # counts globally already; leaving it out of the per-strategy view
    # would let a strategy open a second name beside a position that may
    # well be its own. `strategy_symbols_for` folds this into every slot.
    claimed = frozenset().union(*strategy_symbols.values()) if strategy_symbols else frozenset()
    unattributed = (open_symbols | pending_symbols) - claimed
    unattributed |= unattributed_pending

    return EntryLimitState(
        max_open_positions=max_positions,
        max_daily_entries=max_entries,
        open_position_symbols=open_symbols,
        pending_entry_symbols=pending_symbols,
        daily_entry_count=_daily_entry_count(
            conn, trading_day=trading_day,
            exclude_internal_order_id=exclude_internal_order_id),
        trading_day=trading_day,
        max_positions_per_strategy=max_per_strategy,
        strategy_symbols=strategy_symbols,
        unattributed_symbols=frozenset(unattributed),
    )
