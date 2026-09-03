#!/usr/bin/env python3
"""Run the independent scanners (spec sections 5 and F).

This is the operational entry point cron/systemd invokes. It is a thin
wrapper over `scanners.runner.main` -- the logic lives there so it is
importable and testable -- and exists so that scheduled invocations name
a script under `scripts/`, matching every other operational entry point
in this repository.

    scripts/run_scanners.py --profile premarket
    scripts/run_scanners.py --profile open
    scripts/run_scanners.py --profile daily
    scripts/run_scanners.py --scanners orb --limit 50

Runs nothing on a market holiday unless `--ignore-market-calendar` is
given, and never places, sizes or authorises an order.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanners.runner import main  # noqa: E402


def daily_bars_only(argv=None) -> bool:
    """Does this invocation read ONLY daily bars?

    `registry.DAILY_SCANNERS` already says these three "only need daily
    bars… should not pay for minute data it will not read". The provider
    was chosen by SESSION, not by that requirement, so the daily profile
    -- which runs at 16:17 ET, inside AFTER_HOURS -- took the KIS
    per-symbol path and paid for minute data it never read:

        2026-09-02  universe=11047  provider=kis  duration=48798.8s

    Thirteen and a half hours, serialised on the shared ~3s KIS read
    interval, against a budget S6 needs live. The scanners were right
    about what they needed; nothing was asking them.

    Answered from the REQUEST rather than the clock, and deliberately
    strict: every scanner named must be a daily-bars-only one. A mixed
    run still takes the session provider, because one intraday scanner
    in the set means minute bars really are needed.
    """
    from scanners.registry import DAILY_SCANNERS
    from scanners.runner import PROFILES

    argv = list(sys.argv[1:] if argv is None else argv)
    profile = scanners = None
    tokens = iter(range(len(argv)))
    for i in tokens:
        token = argv[i]
        if token == "--profile":
            profile = argv[i + 1] if i + 1 < len(argv) else None
        elif token.startswith("--profile="):
            profile = token.split("=", 1)[1]
        elif token == "--scanners":
            scanners = argv[i + 1] if i + 1 < len(argv) else None
        elif token.startswith("--scanners="):
            scanners = token.split("=", 1)[1]

    if scanners:
        names = [n.strip() for n in scanners.split(",") if n.strip()]
    elif profile:
        names = list(PROFILES.get(profile) or ())
    else:
        # No profile and no scanner list: the caller has not said what it
        # wants, so nothing here narrows anything.
        return False
    return bool(names) and set(names) <= set(DAILY_SCANNERS)


def session_provider(argv=None):
    """The bar provider for the session we are in, or None.

    Built HERE rather than inside `scanners/` on purpose. The extended
    sessions need KIS-backed bars -- S6's premarket scan on 2026-08-31
    read universe 83, DATA_ERROR 77, evaluated 6, signals 0, because the
    default provider has no usable premarket intraday data. But choosing
    it inside the scanner package would mean that package importing a
    broker, and `tests/test_scanner_trading_isolation.py` forbids that:
    an import that does not exist cannot be reached by a path nobody
    thought of. The scanner observes; it does not acquire the capability
    to trade in order to read a bar.

    Returns None on anything unexpected, which leaves the runner on its
    own default -- the behaviour that existed before this function. A
    scan that cannot start because provider selection failed would be
    strictly worse than one running on the previous provider.
    """
    try:
        # Asked BEFORE the session, and before a broker is constructed.
        # A daily-bars-only scan has no use for KIS bars in ANY session,
        # so there is nothing for the session branch to decide.
        if daily_bars_only(argv):
            return None

        from market_data.kis_bar_provider import (
            KIS_AUTHORITATIVE_SESSIONS, provider_for_session,
        )
        from scanners.base import scan_session

        session = scan_session.session_at()
        if session not in KIS_AUTHORITATIVE_SESSIONS:
            return None

        from brokers.kis_broker import KISBroker

        return provider_for_session(session, broker=KISBroker())
    except Exception:  # noqa: BLE001 - falling back is the safe direction
        import logging

        logging.getLogger(__name__).warning(
            "could not build a session bar provider; the scanner will use "
            "its default", exc_info=True)
        return None


if __name__ == "__main__":
    sys.exit(main(provider=session_provider()))
