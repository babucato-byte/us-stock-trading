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


def session_provider():
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
