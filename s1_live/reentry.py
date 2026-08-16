"""Re-entry and duplicate-signal guard for S1 entries.

Four blocks are unconditional and need no tuned number:

    the signal predates the last exit      a stale thesis
    the signal id was already used         the same observation, twice
    the symbol is already held             this is an add, not an entry
    an order for it is already open        the first one has not resolved

A cooldown DURATION is not among them. No validated cooldown exists in
this project, so PHASE 4A §12 forbids inventing one: `cooldown_seconds`
defaults to None and the guard simply does not apply that check. What it
does instead is record `last_exit_at` and `last_exit_reason` so the
number can be chosen from real S1 behaviour later.

Why "signal older than the last exit" is the load-bearing one
-------------------------------------------------------------
It is what makes a cooldown of zero survivable. A scanner that fires
daily will happily re-offer a symbol the morning after it was stopped
out, using yesterday's signal. That is not a new thesis, it is the same
one the market already answered. Comparing the signal's timestamp to the
exit's catches that without needing to guess how long "long enough" is.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ALLOW = "ALLOW"
BLOCK = "BLOCK"

REASON_SIGNAL_PREDATES_EXIT = "SIGNAL_PREDATES_LAST_EXIT"
REASON_DUPLICATE_SIGNAL = "DUPLICATE_SOURCE_SIGNAL_ID"
REASON_ALREADY_HELD = "SYMBOL_ALREADY_HELD"
REASON_OPEN_ORDER = "OPEN_ORDER_EXISTS"
REASON_COOLDOWN = "REENTRY_COOLDOWN"
REASON_DAILY_SYMBOL_LIMIT = "SYMBOL_DAILY_ENTRY_LIMIT"
REASON_STATE_UNKNOWN = "REENTRY_STATE_UNKNOWN"


@dataclass
class SymbolState:
    """What is known about this symbol's recent history.

    Every field is Optional and None means "not recorded", never "zero"
    or "never happened". A caller that cannot establish the state should
    say so with `known=False` rather than passing an empty record, which
    would read as a symbol with a clean history.
    """

    symbol: str
    known: bool = True
    last_entry_at: Optional[datetime] = None
    last_exit_at: Optional[datetime] = None
    last_exit_reason: Optional[str] = None
    cooldown_until: Optional[datetime] = None
    entries_today: int = 0
    used_signal_ids: frozenset = frozenset()
    currently_held: bool = False
    has_open_order: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "known": self.known,
            "last_entry_at": _iso(self.last_entry_at),
            "last_exit_at": _iso(self.last_exit_at),
            "last_exit_reason": self.last_exit_reason,
            "cooldown_until": _iso(self.cooldown_until),
            "entries_today": self.entries_today,
            "used_signal_id_count": len(self.used_signal_ids),
            "currently_held": self.currently_held,
            "has_open_order": self.has_open_order,
        }


@dataclass(frozen=True)
class ReentryResult:
    verdict: str
    reason_code: Optional[str] = None
    detail: str = ""

    @property
    def allows_entry(self) -> bool:
        return self.verdict == ALLOW

    def as_dict(self):
        return {"verdict": self.verdict, "reason_code": self.reason_code,
                "detail": self.detail}


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else None


def _aware(value):
    """Shared with `freshness.as_utc` -- one definition of "a timestamp".

    This module used to accept only `datetime`, which meant every
    candidate row (whose timestamps come off a CSV as ISO strings) was
    rejected for having "no usable signal timestamp". Two parsers, two
    ideas of the same thing.
    """
    from s1_live.freshness import as_utc

    return as_utc(value)


def check(*, state: SymbolState, source_signal_id, source_signal_timestamp,
          now=None, cooldown_seconds: Optional[int] = None,
          max_entries_per_symbol_per_day: Optional[int] = None) -> ReentryResult:
    """May this signal open a NEW position in this symbol?"""
    current = _aware(now) or datetime.now(timezone.utc)

    if state is None or not getattr(state, "known", False):
        return ReentryResult(BLOCK, REASON_STATE_UNKNOWN,
                             "the symbol's re-entry history could not be established")

    if state.currently_held:
        return ReentryResult(BLOCK, REASON_ALREADY_HELD,
                             f"{state.symbol} is already held; this would be an add, "
                             "not an entry")
    if state.has_open_order:
        return ReentryResult(BLOCK, REASON_OPEN_ORDER,
                             f"an order for {state.symbol} is already open")

    signal_id = str(source_signal_id or "").strip()
    if not signal_id:
        return ReentryResult(BLOCK, REASON_STATE_UNKNOWN,
                             "the candidate carries no source_signal_id")
    if signal_id in (state.used_signal_ids or frozenset()):
        return ReentryResult(BLOCK, REASON_DUPLICATE_SIGNAL,
                             f"signal {signal_id} has already been acted on")

    stamp = _aware(source_signal_timestamp)
    if stamp is None:
        return ReentryResult(BLOCK, REASON_STATE_UNKNOWN,
                             "the candidate carries no usable signal timestamp")
    exited = _aware(state.last_exit_at)
    if exited is not None and stamp <= exited:
        return ReentryResult(
            BLOCK, REASON_SIGNAL_PREDATES_EXIT,
            f"the signal ({stamp.isoformat()}) is not newer than the last exit "
            f"({exited.isoformat()}, {state.last_exit_reason or 'reason unrecorded'})")

    # Cooldown is applied ONLY when a duration was configured, or when a
    # cooldown_until was already recorded by whoever set it.
    until = _aware(state.cooldown_until)
    if until is None and cooldown_seconds is not None and exited is not None:
        until = exited + timedelta(seconds=int(cooldown_seconds))
    if until is not None and current < until:
        return ReentryResult(BLOCK, REASON_COOLDOWN,
                             f"{state.symbol} is in cooldown until {until.isoformat()}")

    if (max_entries_per_symbol_per_day is not None
            and state.entries_today >= int(max_entries_per_symbol_per_day)):
        return ReentryResult(BLOCK, REASON_DAILY_SYMBOL_LIMIT,
                             f"{state.symbol} already had {state.entries_today} "
                             f"entries today")

    return ReentryResult(ALLOW)
