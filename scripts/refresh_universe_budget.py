#!/usr/bin/env python
"""Read the KIS account balance and persist it as the universe budget.

This is the ONLY entry point in the T8 path that talks to a real account,
kept as its own script so the rest of the universe build stays a pure,
network-free, fully testable transformation (T8: "실계좌 조회 부분은 fake
session 테스트 + 실행 스크립트 분리").

Requires, on the machine that runs it:

    KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO   (secrets, never in git)
    KIS_ACCOUNT_READ_ENABLED=true                   (read gate; order gate is separate)

It reads only. It never submits, cancels, or amends an order, and it does
not touch KIS_LIVE_ORDER_ENABLED.

Usage:
    venv/bin/python scripts/refresh_universe_budget.py [--show]

Exit codes: 0 = balance read and persisted, 1 = read failed but a previous
value is being kept, 2 = read failed and there is no previous value (the
filtered universe cannot be rebuilt).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from universe_budget import refresh_budget  # noqa: E402
from universe_filter import UniverseBudget  # noqa: E402


def build_broker():
    from brokers.kis_broker import KISBroker

    return KISBroker()


def main(argv=None, *, broker=None, state_path=None, logger=print):
    parser = argparse.ArgumentParser(description="Refresh the universe account budget from KIS.")
    parser.add_argument("--show", action="store_true",
                        help="print the resulting budget and derived price ceiling as JSON")
    parser.add_argument("--state-file", default=None,
                        help="override the persisted state path (default: state/universe_budget.json)")
    args = parser.parse_args(argv)

    path = state_path if state_path is not None else args.state_file

    if broker is None:
        try:
            broker = build_broker()
        except Exception as exc:  # noqa: BLE001 -- config/credential problems are read failures too
            logger(f"[UNIVERSE BUDGET] cannot construct KIS broker: {type(exc).__name__}: {exc}")

            class _Unavailable:
                def get_account_snapshot(self):
                    raise RuntimeError(f"KIS broker unavailable: {exc}")

            broker = _Unavailable()

    state, error = refresh_budget(broker, path=path, logger=logger)

    if state is None:
        logger("[UNIVERSE BUDGET] no budget available; the filtered universe cannot be rebuilt.")
        return 2

    if args.show:
        budget: UniverseBudget = state.to_budget()
        logger(json.dumps({
            "available_cash_usd": state.available_cash_usd,
            "as_of": state.as_of,
            "source": state.source,
            "stale": state.stale,
            "cash_usage_percent": budget.effective_cash_usage_percent,
            "position_rate": budget.position_rate,
            "price_ceiling_usd": budget.price_ceiling_usd,
        }, indent=2, sort_keys=True))

    return 1 if error is not None else 0


if __name__ == "__main__":
    sys.exit(main())
