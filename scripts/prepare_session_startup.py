"""Prepare the next session's startup universe, before the session opens.

Runs on a plain cron tick and is a NO-OP for all but a few minutes before
each session boundary -- `session_startup.prep_due()` decides, from the
production session truth, whether this tick is inside the window.

What it does is deliberately small: it calls the symbol selection the
collector already uses, and writes the answer where both the collector and
the scanner can read it. It introduces no selection logic of its own.

    market_data.bootstrap_watchlist.build()   <- unchanged, already
                                                 session-generic, already
                                                 bounded by the real
                                                 subscription ceiling
                    |
            startup artifact (atomic)
                    |
         +----------+----------+
      collector              scanner

This lives in `scripts/` rather than in `scanners/` because `build()`
reaches `state_store` and the scanner package must not
(tests/test_scanner_trading_isolation.py). The scanner reads the JSON; it
never reaches the builder.

Never submits an order, never constructs a BUY intent, never touches a
broker. It reads a ranking, a manifest and the position store, and writes
one file.
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

logger = logging.getLogger("session_startup_prep")

#: Which session seeds which, for the collector's "prior session" input.
#: The same mapping the collector wrapper already uses -- premarket seeds
#: from the PREVIOUS day's after-hours, the rest from the one before them
#: on the same day.
_PRIOR = {
    "PREMARKET": ("AFTER_HOURS", -1),
    "REGULAR": ("PREMARKET", 0),
    "AFTER_HOURS": ("REGULAR", 0),
    "OVERNIGHT_DAYTIME": ("AFTER_HOURS", 0),
}


def _discovery_root():
    """Where the shared artifacts live, derived the way s6_scan.sh derives
    the manifest path: a sibling of the candidate directory the release env
    already resolved. Never the release directory -- runtime state written
    there vanishes on the next deploy."""
    candidates = os.environ.get("SCANNER_CANDIDATE_DIR")
    if not candidates:
        return None
    return os.path.join(os.path.dirname(candidates.rstrip("/")), "discovery")


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-session", default=None,
                        help="prepare this session regardless of the clock "
                             "(operational recovery; the cron never sets it)")
    parser.add_argument("--discovery-root", default=None)
    args = parser.parse_args(argv)

    from datetime import timedelta

    from market_data import bootstrap_watchlist as bootstrap
    from market_data import kis_hdfscnt0 as wire
    from market_hours import us_trading_day
    from scanners.base import session_startup

    now = datetime.now(timezone.utc)
    root = args.discovery_root or _discovery_root()
    if not root:
        print("SESSION_STARTUP_PREP_INVALID reason=NO_DISCOVERY_ROOT")
        return 1

    if args.force_session:
        target, boundary = str(args.force_session).upper(), None
    else:
        target, boundary = session_startup.prep_due(now)
    if target is None:
        # The ordinary answer. Printed so a tick that did nothing is
        # distinguishable from a tick that failed.
        nxt, when = session_startup.next_boundary(now)
        print("SESSION_STARTUP_PREP_NOOP next_session=%s boundary=%s" % (
            nxt, when.isoformat() if when else None))
        return 0

    # The trading day the TARGET session belongs to, not today's. A
    # premarket that opens after midnight UTC belongs to the day it trades.
    target_day = us_trading_day(boundary or now)
    # The COLLECTOR's ceiling, and only the collector's. The scanner view
    # this artifact also carries is deliberately uncapped -- the strategy
    # universe is the manifest, not what one websocket can stream.
    cap = wire.MAX_SUBSCRIPTIONS

    print("SESSION_STARTUP_PREP_START target_session=%s trading_day=%s "
          "boundary=%s cap=%s" % (target, target_day,
                                  boundary.isoformat() if boundary else None, cap))

    existing, status = session_startup.load_valid(
        root, trading_day=target_day, target_session=target, cap=cap, now=now)
    if existing is not None and existing.get("prepared_normally"):
        # Idempotent: a second tick inside the same window must not
        # re-derive a list the first already wrote, because two different
        # lists for one session is the disagreement this removes.
        print("SESSION_STARTUP_PREP_COMPLETE target_session=%s trading_day=%s "
              "symbol_count=%s reused=true" % (
                  target, target_day, existing.get("symbol_count")))
        return 0

    prior_session, day_offset = _PRIOR.get(target, (None, 0))
    prior_day = us_trading_day(
        (boundary or now) + timedelta(days=day_offset)) if prior_session else None

    try:
        pairs, why = bootstrap.build(session=target, trading_day=target_day,
                                     prior_session=prior_session,
                                     prior_trading_day=prior_day)
    except Exception as exc:  # noqa: BLE001 - a failed prep must never stop a session
        print("SESSION_STARTUP_PREP_INVALID target_session=%s trading_day=%s "
              "reason=BUILD_FAILED detail=%r" % (target, target_day, exc))
        return 1

    symbols = [symbol for symbol, _exchange in pairs]
    if not symbols:
        print("SESSION_STARTUP_PREP_INVALID target_session=%s trading_day=%s "
              "reason=NO_SYMBOLS detail=%s" % (target, target_day, json.dumps(why)))
        return 1

    artifact = session_startup.build_artifact(
        trading_day=target_day, target_session=target, symbols=symbols,
        cap=cap, source=session_startup.SOURCE_PREPARED,
        source_session=prior_session,
        source_manifest_generation=why.get("manifest_generation"),
        source_manifest_created_at=why.get("manifest_created_at"),
        selection_source=json.dumps(
            {k: why.get(k) for k in ("from_held_positions", "from_prior_session",
                                     "from_coarse_discovery", "from_manifest",
                                     "cap", "unused_slots")}, sort_keys=True),
        scanner_view=session_startup.build_scanner_view(target),
        now=now)
    path = session_startup.write_atomic(root, artifact)

    print("SESSION_STARTUP_PREP_COMPLETE target_session=%s trading_day=%s "
          "symbol_count=%s cap=%s path=%s reused=false" % (
              target, target_day, artifact["symbol_count"], cap, path))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(run())
