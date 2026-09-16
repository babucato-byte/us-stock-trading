"""Was the internal side still the same state the broker snapshot was compared against?

The race this exists to close
-----------------------------
`build_snapshot` reads KIS first and this codebase second. The three KIS
reads each queue behind the shared 3-second READ limiter, so the gap
between "the broker told us what is open" and "we looked at our own
ledger" is seconds, and under contention has been tens of seconds.

An order that is ACCEPTED when KIS is read and CANCELLED when the ledger
is read drops out of `_INTERNAL_LIVE_STATUSES`, so it is absent from
`internal_ids` while still present in the broker's open-order list. The
comparison then reports it as an order "not tracked internally" -- an
order this codebase submitted, tracked correctly, and cancelled
correctly.

Confirmed twice on 2026-09-16:

    HAL  SELL 0030499393   KIS OPEN observed, internal ACCEPTED -> CANCELLED
    ARQT BUY  0030578574   KIS OPEN observed, ACCEPTED -> CANCEL_PENDING -> CANCELLED

Both recovered to CLEAN on the next pass, which is the signature of a
cross-time artifact rather than a real disagreement.

Why a marker and not a transaction
----------------------------------
The broker reads cannot be inside a database transaction -- they are
network calls taking seconds, and holding a write lock across them would
stall every writer this is trying to observe. So instead of preventing
the change, this DETECTS it: capture the state of the consistency domain
before the broker reads, capture it again before the comparison, and
refuse to publish a mismatch built across a change.

The marker reuses what CODEX-047 already built rather than adding a
counter of its own: `order_state_events` is append-only with an
autoincrement id and is written in the SAME transaction as every
accepted state change, and `kis_order_idempotency.version` is the
optimistic-concurrency counter every transition bumps. A state change
that did not move one of those did not pass the state machine.

The domain is deliberately small
--------------------------------
Only the state the comparison actually reads. A scanner writing a
candidate, an observation row, an activity ranking -- none of it can
make a broker-vs-ledger comparison wrong, and invalidating on those
would turn a busy scan into a permanently unverifiable account. See
`DOMAIN_FIELDS` for exactly what is watched and `describe_change` for
what a caller is told changed.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

STALE_REASON_CODE = "RECONCILIATION_SNAPSHOT_STALE"

#: What each watched table contributes to the marker, and why.
#:
#: Every entry corresponds to something the snapshot comparison reads. A
#: field is here because changing it can flip a mismatch verdict, not
#: because it happens to live on the same row.
DOMAIN_FIELDS = {
    # The order ledger the open-order and fill checks compare against.
    # `status` is the field that produced both confirmed incidents;
    # `broker_order_id` is the mapping the comparison joins on, so a
    # late-learned id changes which orders match.
    "orders": (
        "kis_order_idempotency",
        "SELECT internal_order_id, broker_order_id, status, version "
        "FROM kis_order_idempotency ORDER BY internal_order_id",
    ),
    # The append-only transition history. Same transaction as the state
    # change, so its high-water mark moves for every accepted
    # transition -- including ones that land back on the same status.
    "order_events": (
        "order_state_events",
        "SELECT COALESCE(MAX(event_id), 0), COUNT(*) FROM order_state_events",
    ),
    # General lifecycle positions: OPEN/CLOSED transitions and fill
    # settlement both land here, and both move the position comparison.
    "positions": (
        "positions",
        "SELECT position_id, state, filled_qty, remaining_qty, broker_order_id "
        "FROM positions ORDER BY position_id",
    ),
    # S6's own book. A strategy lives in exactly one store, and S6 is
    # the one currently trading, so omitting it would leave the live
    # strategy's transitions invisible to the guard.
    "s6_positions": (
        "s6_positions",
        "SELECT position_id, status, quantity, exit_submitted, "
        "entry_order_id FROM s6_positions ORDER BY position_id",
    ),
    # Exit intents that reserve a broker order. A transition here is an
    # exit appearing or completing, which changes which orders the
    # comparison expects to be live.
    "exit_intents": (
        "exit_intents",
        "SELECT intent_id, state, broker_order_id, confirmed_filled_qty "
        "FROM exit_intents ORDER BY intent_id",
    ),
}


@dataclass(frozen=True)
class ConsistencyMarker:
    """A digest per watched table. Comparable, and cheap to carry."""

    digests: Tuple[Tuple[str, str], ...]

    def as_dict(self):
        return dict(self.digests)

    def changed_domains(self, other) -> Tuple[str, ...]:
        if other is None:
            return ()
        mine, theirs = self.as_dict(), other.as_dict()
        names = sorted(set(mine) | set(theirs))
        return tuple(n for n in names if mine.get(n) != theirs.get(n))


def capture(conn) -> Optional[ConsistencyMarker]:
    """The current state of the consistency domain.

    Returns None when the marker cannot be taken at all, which the
    caller must treat as "no guard available" rather than as "nothing
    changed" -- `is_stale` enforces that direction.

    A table that does not exist contributes a fixed sentinel rather than
    being skipped: a schema where `exit_intents` is absent is a valid
    older deployment, and it must not silently look identical to one
    where the table exists and is empty.
    """
    if conn is None:
        return None
    digests = []
    try:
        for name, (_table, sql) in sorted(DOMAIN_FIELDS.items()):
            digests.append((name, _digest(conn, sql)))
    except Exception:  # noqa: BLE001 - a marker that cannot be taken is None
        logger.warning("could not capture the reconciliation consistency "
                       "marker; this pass has no cross-time guard",
                       exc_info=True)
        return None
    return ConsistencyMarker(digests=tuple(digests))


def _digest(conn, sql) -> str:
    try:
        rows = conn.execute(sql).fetchall()
    except Exception as exc:  # noqa: BLE001 - missing table, older schema
        name = type(exc).__name__
        return f"UNAVAILABLE:{name}"
    hasher = hashlib.sha256()
    for row in rows:
        for value in row:
            hasher.update(b"\x1f")
            hasher.update(repr(value).encode("utf-8", "replace"))
        hasher.update(b"\x1e")
    return hasher.hexdigest()[:32]


def is_stale(before, after) -> bool:
    """Whether the comparison would be built across a state change.

    Fail-closed on a missing marker: if either capture failed, this
    cannot prove the state held still, and a snapshot that cannot be
    proven consistent must not publish a mismatch. Returning False there
    would restore exactly the behaviour being fixed, and silently.
    """
    if before is None or after is None:
        return True
    return bool(before.changed_domains(after))


def describe_change(before, after) -> str:
    """What moved, for the audit trail. Never used for a decision."""
    if before is None or after is None:
        return ("the consistency marker could not be captured, so this pass "
                "cannot prove the internal state held still")
    domains = before.changed_domains(after)
    if not domains:
        return "no change"
    return ("internal state changed during broker collection: "
            + ", ".join(domains))
