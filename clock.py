"""CODEX-030: an explicit Clock dependency for every wall-clock-sensitive
piece of trading logic (position lifecycle, market session gates, EOD
forced close).

Before this module, `positions/lifecycle.py::check_and_manage()` (and
friends) called `market_hours.eastern_now()` -- the *real* system clock --
whenever a caller passed `now=None`. Production code is supposed to do
exactly that. The bug was that several tests ALSO passed `now=None` (or
omitted `now` entirely) for scenarios that had nothing to do with EOD
behavior (target-hit, stop-loss, no-action), which meant those tests
silently depended on what time of day the test happened to run: within
`EOD_FORCE_CLOSE_MINUTES_BEFORE_CLOSE` minutes of the real 16:00 ET close,
`check_and_manage()`'s EOD cutoff check fires unconditionally (by design --
it is a safety override that always takes priority over price-based exits),
turning a target/stop test into an EOD_FORCED_CLOSE failure that had
nothing to do with a code defect.

`Clock` makes "what time is it" an explicit, injectable dependency instead
of an ambient global, so a test can hand lifecycle code a `FrozenClock`
fixed to an unambiguous mid-session moment and get the same result no
matter when the test suite actually runs.
"""

from datetime import datetime, timezone

from market_hours import EASTERN


class Clock:
    """Abstract time source. Every method must return a timezone-aware
    datetime (or date) -- naive datetimes are rejected by FrozenClock's
    constructor so a test can never accidentally construct one that
    silently means something different depending on the interpreter's
    local timezone."""

    def now_utc(self):  # pragma: no cover - abstract
        raise NotImplementedError

    def now_eastern(self):  # pragma: no cover - abstract
        raise NotImplementedError

    def market_date(self):
        """The US-Eastern calendar date `now_eastern()` falls on -- the
        date lifecycle/session logic should treat as "today" for EOD
        cutoffs, order_date defaulting, and holiday/session-gate checks."""
        return self.now_eastern().date()


class ProductionClock(Clock):
    """The real wall clock. This is the default everywhere in production
    code -- no behavior change from before this module existed."""

    def now_utc(self):
        return datetime.now(timezone.utc)

    def now_eastern(self):
        return datetime.now(EASTERN)


class FrozenClock(Clock):
    """A fixed point in time for deterministic tests. Accepts either an
    explicit Eastern-zoned `now_eastern` (preferred -- this is the zone
    every session/EOD decision is actually made in) or a UTC `now_utc`;
    supplying neither is a programming error (a frozen clock frozen to
    nothing is not useful), and supplying a naive datetime for either is
    rejected outright rather than silently guessing which zone it means.
    """

    def __init__(self, now_eastern=None, now_utc=None):
        if now_eastern is None and now_utc is None:
            raise ValueError("FrozenClock requires now_eastern and/or now_utc")
        for label, value in (("now_eastern", now_eastern), ("now_utc", now_utc)):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"FrozenClock.{label} must be timezone-aware, got a naive datetime")
        if now_eastern is not None:
            self._eastern = now_eastern.astimezone(EASTERN)
        else:
            self._eastern = now_utc.astimezone(EASTERN)
        if now_utc is not None:
            self._utc = now_utc.astimezone(timezone.utc)
        else:
            self._utc = now_eastern.astimezone(timezone.utc)

    def now_utc(self):
        return self._utc

    def now_eastern(self):
        return self._eastern


# The process-wide default. Production call sites resolve `clock or
# DEFAULT_CLOCK` rather than importing this directly, so a test can pass
# its own FrozenClock per-call without any module-level monkeypatching.
DEFAULT_CLOCK = ProductionClock()
