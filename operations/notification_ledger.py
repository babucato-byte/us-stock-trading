"""Send a thing once, not once a minute.

The problem
-----------
The trading engine ticks every minute and most facts it observes stay
true for hours. "DT is still EXIT_PENDING" was true for 105 consecutive
minutes; a channel that says so 105 times is a channel nobody reads,
and the cost of that is not noise -- it is the genuinely new message
that gets scrolled past.

Claim, then send
----------------
`claim()` is the whole mechanism: `INSERT OR IGNORE` on a PRIMARY KEY
either writes the row or does not, atomically. Two processes racing on
one event resolve without a lock, and a cron restart cannot re-send what
the previous run already sent, because the row outlives the process.

Claiming BEFORE sending is deliberate. The opposite order -- send, then
record -- duplicates every message whose process died between the two,
which is exactly the restart case §21 names. The cost is that a delivery
failure would otherwise be silently swallowed, so `release()` exists:
a caller whose send definitively failed gives the claim back and the
next tick tries again. A send whose outcome is UNKNOWN keeps its claim,
because a possible duplicate is worse than a possible miss for something
an operator will see either way in the position state.

State versions
--------------
A key includes a `state_version` so the SAME event about the SAME
position can legitimately recur when something actually changed -- an
EXIT_PENDING reminder after thirty minutes is a different notification
from the original, and says so in its key rather than by suppressing
the check.

Never fatal
-----------
Every function swallows its own failures and errs toward SENDING. A
broken ledger must not silence a reconciliation alert: the failure mode
of this module is a duplicate message, never a missing one.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _now(now=None):
    return now or datetime.now(timezone.utc)


def key_for(event_type, *, strategy_id=None, symbol=None, subject_id=None,
            state_version=None) -> str:
    """A deterministic identity for one notification.

    Deterministic across processes and restarts -- the point is that a
    second process computing the same key gets the same string, so
    hashing must not involve anything process-local.
    """
    parts = [str(event_type or ""), str(strategy_id or ""),
             str(symbol or "").upper(), str(subject_id or ""),
             str(state_version or "")]
    raw = "|".join(parts)
    # The readable prefix survives in the DB so an operator can grep the
    # ledger for a symbol without reversing a hash.
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{event_type}:{str(symbol or '-').upper()}:{digest}"


def claim(conn, key, *, event_type, strategy_id=None, symbol=None,
          subject_id=None, state_version=None, channel=None,
          event_time=None, now=None) -> bool:
    """Reserve the right to send this notification. True if it is ours.

    False means somebody already sent it -- this process, an earlier
    tick, or a run that died before restarting. Errs toward True: a
    ledger that cannot be read must not silence an alert.
    """
    current = _now(now)
    delay = None
    if event_time is not None:
        try:
            moment = (event_time if isinstance(event_time, datetime)
                      else datetime.fromisoformat(str(event_time)))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            delay = (current - moment).total_seconds()
        except (TypeError, ValueError):
            delay = None
    try:
        changed = conn.execute(
            "INSERT OR IGNORE INTO notification_ledger ("
            "notification_key, event_type, strategy_id, symbol, subject_id, "
            "state_version, channel, event_time, sent_at, delay_seconds, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (key, event_type, strategy_id, str(symbol or "").upper() or None,
             subject_id, state_version, channel,
             event_time.isoformat() if isinstance(event_time, datetime)
             else (str(event_time) if event_time else None),
             current.isoformat(), delay, current.isoformat())).rowcount
        conn.commit()
        return bool(changed)
    except Exception:  # noqa: BLE001 - a broken ledger must not silence
        # an alert. Duplicate beats missing.
        logger.warning("notification ledger unavailable for %s; sending anyway",
                       key, exc_info=True)
        return True


def release(conn, key) -> bool:
    """Give a claim back after a delivery DEFINITELY failed.

    Only for a definite failure. A send whose outcome is unknown keeps
    its claim: re-sending something that may already have arrived is the
    behaviour this module exists to prevent.
    """
    try:
        changed = conn.execute(
            "DELETE FROM notification_ledger WHERE notification_key = ?",
            (key,)).rowcount
        conn.commit()
        return bool(changed)
    except Exception:  # noqa: BLE001
        logger.warning("could not release notification claim %s", key,
                       exc_info=True)
        return False


def already_sent(conn, key) -> bool:
    """Read-only check. Errs toward False (i.e. toward sending)."""
    try:
        row = conn.execute(
            "SELECT 1 FROM notification_ledger WHERE notification_key = ?",
            (key,)).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def delay_for(conn, key) -> Optional[float]:
    """How late this notification was, in seconds after its event."""
    try:
        row = conn.execute(
            "SELECT delay_seconds FROM notification_ledger "
            "WHERE notification_key = ?", (key,)).fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


def last_sent(conn, *, event_type, symbol=None, subject_id=None):
    """The most recent send of this event for this subject, or None.

    Used by the reminder rules: "has it been thirty minutes" needs the
    previous send, not merely whether one happened.
    """
    where = ["event_type = ?"]
    params = [event_type]
    if symbol:
        where.append("symbol = ?")
        params.append(str(symbol).upper())
    if subject_id:
        where.append("subject_id = ?")
        params.append(subject_id)
    try:
        return conn.execute(
            "SELECT * FROM notification_ledger WHERE " + " AND ".join(where) +
            " ORDER BY sent_at DESC LIMIT 1", params).fetchone()
    except Exception:  # noqa: BLE001
        return None
