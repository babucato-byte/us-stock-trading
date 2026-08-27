"""When a per-minute engine should actually say something.

The engine ticks every minute; most of what it observes stays true for
hours. DT sat in EXIT_PENDING for 105 consecutive minutes. A channel
that reports that 105 times is not thorough, it is unreadable -- and the
cost is the genuinely new message that gets scrolled past.

So: the engine runs every minute, and this decides what is worth
saying. Three rules, and nothing else:

  TRANSITIONS      a state that CHANGED is news. WATCHING -> READY is
                   sent; WATCHING -> WATCHING is not.

  ONE-SHOTS        BUY_SUBMITTED, BUY_FILLED, EXIT_SIGNAL,
                   SELL_SUBMITTED, SELL_FILLED happen once and are sent
                   once, keyed on the order or position they belong to
                   so a restart cannot repeat them.

  BOUNDED REMINDERS  a state that persists may be re-stated, but only
                   on a condition that is itself a change: enough time
                   passed, the session turned over, or the thing
                   blocking it became possible. Never merely because
                   another minute went by.

Timestamps are facts about different moments
--------------------------------------------
`event_time` is when the thing happened. `sent_at` is when we said so.
`delay_seconds` is the gap. A message that reports its own send time as
though it were the fill time makes a late notification indistinguishable
from a late fill, which is the confusion §22 exists to prevent.

Deciding, not sending
---------------------
Nothing here calls Slack. `should_notify` answers yes or no and
`mark_sent` records it; delivery stays with `live_notifications`, which
already handles routing, redaction and health tracking.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from operations import notification_ledger as ledger

logger = logging.getLogger(__name__)

#: How long a persistent state may go unmentioned before one reminder.
#: Thirty minutes is the interval §18 names; it is a reporting cadence,
#: not a trading threshold, and nothing decides an order from it.
REMINDER_AFTER_SECONDS = 30 * 60

#: Delay past which a notification is itself a system problem (§24).
#: Reused from the existing operational bound rather than chosen here.
LATE_NOTIFICATION_SECONDS = 5 * 60

# Events that occur once per subject and must never repeat.
ONE_SHOT_EVENTS = frozenset({
    "BUY_SUBMITTED", "BUY_FILLED", "EXIT_SIGNAL",
    "SELL_SUBMITTED", "SELL_FILLED", "CLOSED",
})

# States whose persistence may earn a bounded reminder.
REMINDABLE_STATES = frozenset({"EXIT_PENDING"})


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _as_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def state_changed(previous, current) -> bool:
    """Is this a transition, or the same fact restated?"""
    return (previous or None) != (current or None)


def should_notify(conn, *, event_type, symbol=None, strategy_id=None,
                  subject_id=None, previous_state=None, current_state=None,
                  state_version=None, event_time=None, now=None,
                  channel=None) -> Dict[str, Any]:
    """Decide, claim if yes, and say why either way.

    Returns {"send", "reason", "key", "delay_seconds"}. The reason is
    part of the answer on purpose: a suppressed message that cannot
    explain itself is indistinguishable from a broken notifier.
    """
    current = _now(now)
    version = state_version
    if version is None and current_state is not None:
        version = str(current_state)

    key = ledger.key_for(event_type, strategy_id=strategy_id, symbol=symbol,
                         subject_id=subject_id, state_version=version)

    # A transition is news; the same state restated is not.
    if current_state is not None and not state_changed(previous_state,
                                                       current_state):
        if current_state not in REMINDABLE_STATES:
            return {"send": False, "reason": "NO_STATE_CHANGE", "key": key,
                    "delay_seconds": None}
        decision = _reminder_due(conn, event_type=event_type, symbol=symbol,
                                 subject_id=subject_id, now=current)
        if not decision["due"]:
            return {"send": False, "reason": decision["reason"], "key": key,
                    "delay_seconds": None}
        # A reminder is a DIFFERENT notification from the original and
        # gets its own key, rather than the original's being forgotten.
        key = ledger.key_for(
            event_type, strategy_id=strategy_id, symbol=symbol,
            subject_id=subject_id,
            state_version=f"{version}:reminder:{decision['bucket']}")

    claimed = ledger.claim(
        conn, key, event_type=event_type, strategy_id=strategy_id,
        symbol=symbol, subject_id=subject_id, state_version=version,
        channel=channel, event_time=event_time, now=current)
    if not claimed:
        return {"send": False, "reason": "ALREADY_SENT", "key": key,
                "delay_seconds": None}

    delay = ledger.delay_for(conn, key)
    return {"send": True, "reason": "SEND", "key": key,
            "delay_seconds": delay,
            "late": bool(delay is not None and delay > LATE_NOTIFICATION_SECONDS)}


def _reminder_due(conn, *, event_type, symbol, subject_id, now) -> Dict[str, Any]:
    """Has a persistent state earned one more mention?

    Bucketed by elapsed half-hours rather than compared to a moving
    deadline: the bucket becomes part of the key, so the second reminder
    is a different notification from the first and the thirty-first
    minute cannot produce one per tick.
    """
    previous = ledger.last_sent(conn, event_type=event_type, symbol=symbol,
                               subject_id=subject_id)
    if previous is None:
        return {"due": True, "reason": "FIRST", "bucket": 0}
    sent_at = _as_dt(previous["sent_at"] if hasattr(previous, "keys")
                     else previous[-3])
    if sent_at is None:
        return {"due": True, "reason": "UNKNOWN_LAST_SENT", "bucket": 0}
    elapsed = (now - sent_at).total_seconds()
    if elapsed < REMINDER_AFTER_SECONDS:
        return {"due": False, "reason": "REMINDER_NOT_DUE", "bucket": None}
    return {"due": True, "reason": "REMINDER_DUE",
            "bucket": int(elapsed // REMINDER_AFTER_SECONDS)}


def reminder_on_change(conn, *, event_type, symbol, subject_id,
                       change_reason, now=None, strategy_id=None,
                       channel=None) -> Dict[str, Any]:
    """A reminder earned by something changing rather than by time.

    §18's other two triggers: the session turned over, or the capability
    that was blocking became available. Both are genuine news about a
    state that has not itself changed, and both are rare -- which is why
    they may bypass the clock.
    """
    return should_notify(
        conn, event_type=event_type, symbol=symbol, strategy_id=strategy_id,
        subject_id=subject_id, state_version=f"change:{change_reason}",
        now=now, channel=channel)


def timestamps_for(**moments) -> Dict[str, Any]:
    """Named moments rendered ET+KST, each labelled as itself.

    §22: a message carrying one unlabelled time invites the reader to
    treat it as the trade's. Every moment here keeps its own name.
    """
    from scanners.notify import labels

    out = {}
    for name, value in moments.items():
        if value is None:
            continue
        rendered = labels.dual_time(value)
        out[name] = rendered if rendered else value
    return out
