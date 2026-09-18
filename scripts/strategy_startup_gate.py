"""Hold a pre-evaluable scan invocation until the market can answer it.

Why this exists
---------------
The scanner cron fires on a fixed cadence and a session boundary does not
land on it. On 2026-09-19 the REGULAR boundary was six minutes before the
first tick could produce a meaningful verdict, so that tick rejected every
symbol it looked at for the same structural reason -- the opening range had
not been broken yet -- and the next tick was fifteen minutes later. Between
them sat nine minutes of evaluable market that nothing looked at.

Neither half of that is acceptable for a breakout strategy: scanning too
early spends a cycle on a market that cannot answer, and waiting for the
next tick deliberately blinds the strategy to the window it exists to
trade. So the early invocation waits, and then scans.

What it must not do, and why it is a separate process
-----------------------------------------------------
IT MUST NOT HOLD THE SCANNER LOCK WHILE WAITING. That lock is what stops
two scans publishing candidates built from overlapping bars, and a process
holding it through a six-minute sleep would make every other session's scan
in that window a SCANNER_LOCK_SKIPPED for no reason at all. This runs
BEFORE `flock` in the wrapper and exits before the lock is taken; the wait
is therefore structurally outside it, not merely intended to be.

Waiting is also not free of risk: a lot can stop being true in six minutes.
So nothing learned before the wait is trusted after it. The session, the
trading day and the release the wrapper resolved are all re-derived on wake,
and a disagreement aborts rather than scans.

It prints one word -- PROCEED or ABORT -- and writes named telemetry. It
never scans, never publishes, never touches a broker.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone


def _stamp(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _emit(event, **fields):
    parts = " ".join(f"{k}={_stamp(v)}" for k, v in fields.items())
    print(f"{datetime.now(timezone.utc).isoformat()} {event} {parts}",
          file=sys.stderr)


def _release_identity():
    """What the wrapper resolved, as the wrapper sees it.

    Compared before and after the wait so a deploy that switches the
    release pointers mid-wait cannot have this process scan with one half
    of one release and one half of another.
    """
    return (os.environ.get("TRADING_PROJECT_ROOT"),
            os.environ.get("SCANNER_SHA"),
            os.environ.get("DEPLOYED_COMMIT"),
            os.environ.get("VALIDATED_COMMIT"))


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default=None,
                        help="the session the wrapper resolved; re-derived "
                             "on wake and compared against this")
    parser.add_argument("--max-defer-seconds", type=float, default=None)
    parser.add_argument("--no-wait", action="store_true",
                        help="report the decision without sleeping")
    args = parser.parse_args(argv)

    from market_hours import us_trading_day
    from scanners.base import scan_session, session_startup

    limit = (args.max_defer_seconds if args.max_defer_seconds is not None
             else session_startup.MAX_DEFER_SECONDS)

    invoked_at = datetime.now(timezone.utc)
    timing = session_startup.strategy_timing(invoked_at, session=args.session)
    wait = session_startup.defer_seconds(timing, max_defer_seconds=limit)

    if wait <= 0:
        # The ordinary answer for every invocation but the first after a
        # boundary. Named so "did not wait" is never confused with "the
        # gate did not run".
        _emit("STRATEGY_STARTUP_NO_DEFER",
              session=timing.get("session"),
              invoked_at=invoked_at,
              earliest_evaluable_at=timing.get("earliest_evaluable_at"),
              unavailable_reason=timing.get("unavailable_reason"))
        print("PROCEED")
        return 0

    _emit("STRATEGY_STARTUP_DEFERRED",
          session=timing.get("session"),
          invoked_at=invoked_at,
          earliest_evaluable_at=timing.get("earliest_evaluable_at"),
          defer_seconds=round(wait, 1),
          orb_minutes=timing.get("orb_minutes"),
          min_post_range_bars=timing.get("min_post_range_bars"),
          bar_interval_minutes=timing.get("bar_interval_minutes"))

    before_day = us_trading_day(invoked_at)
    before_session = timing.get("session")
    before_release = _release_identity()

    if not args.no_wait:
        time.sleep(wait)

    resumed_at = datetime.now(timezone.utc)
    earliest = timing["earliest_evaluable_at"]
    lag_ms = (resumed_at - earliest).total_seconds() * 1000.0

    # Nothing from before the wait is trusted now.
    after_session = scan_session.session_at(resumed_at)
    after_day = us_trading_day(resumed_at)
    after_release = _release_identity()

    if after_session != before_session:
        _emit("STRATEGY_STARTUP_DEFER_ABORTED", reason="SESSION_CHANGED",
              was=before_session, now=after_session, resumed_at=resumed_at)
        print("ABORT")
        return 0
    if after_day != before_day:
        _emit("STRATEGY_STARTUP_DEFER_ABORTED", reason="TRADING_DAY_CHANGED",
              was=before_day, now=after_day, resumed_at=resumed_at)
        print("ABORT")
        return 0
    if after_release != before_release:
        _emit("STRATEGY_STARTUP_DEFER_ABORTED", reason="RELEASE_CHANGED",
              resumed_at=resumed_at)
        print("ABORT")
        return 0
    if after_session == scan_session.CLOSED:
        _emit("STRATEGY_STARTUP_DEFER_ABORTED", reason="SESSION_CLOSED",
              resumed_at=resumed_at)
        print("ABORT")
        return 0

    _emit("STRATEGY_STARTUP_RESUMED",
          session=after_session, resumed_at=resumed_at,
          earliest_evaluable_at=earliest, start_lag_ms=round(lag_ms, 1))
    print("PROCEED")
    return 0


if __name__ == "__main__":
    sys.exit(run())
