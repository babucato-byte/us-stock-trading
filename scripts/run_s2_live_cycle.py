#!/usr/bin/env python3
"""One S2 tick. Orchestration only -- it owns no safety decision.

What this does, and what it deliberately does not
-------------------------------------------------
It resolves a session, picks S2's candidate source, calls the SHARED buy
cycle, syncs fills, evaluates S2's exits through the SHARED sell path,
and logs. Every safety judgement -- COMMON_STOCK, orderable cash,
reconciliation, duplicate orders, the kill switch, position limits, the
execution-price gate -- lives inside those shared paths and is not
re-implemented, re-checked or bypassed here.

The order is exits first, then entry. A tick that entered before exiting
would test the position limit against a book it had already made stale.

Fail-closed on entry, continuous on exit
----------------------------------------
Anything unclear blocks a new BUY: an unknown session, an unresolvable
source, a bad configuration. None of it blocks an exit. A position
already held must always be able to leave, and a risk control that also
blocked liquidation would trap the account in the position it exists to
escape.

S1 is not this script's business
--------------------------------
It reads no S1 position and calls no S1 path. If this script fails
entirely, S1's executor, watchdog and exit runtime are unaffected --
they run from their own cron entries against their own store.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("s2_live_cycle")

STATUS_OK = "OK"
STATUS_NO_CANDIDATE = "NO_CANDIDATE"
STATUS_ENTRY_BLOCKED = "ENTRY_BLOCKED"
STATUS_SESSION_CLOSED = "SESSION_NOT_ORDERABLE"
STATUS_ERROR = "ERROR"


def _log_dir() -> Path:
    root = os.environ.get("TRADING_PROJECT_ROOT") or str(REPO_ROOT)
    shared = Path(root).parent / "shared" / "state" / "s2_live_logs"
    target = shared if shared.parent.parent.exists() else Path(root) / "logs" / "s2_live"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _record(payload) -> None:
    try:
        day = payload.get("trading_day") or "unknown"
        with (_log_dir() / f"cycles-{day}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except Exception:  # noqa: BLE001 - a lost log line must not fail a tick
        logger.warning("could not record the S2 cycle", exc_info=True)


def run_once(*, now=None, allow_entry=True) -> dict:
    """The tick. Returns a JSON-serialisable record of what happened."""
    from market_hours import EASTERN
    from scanners.base import scan_session
    from scanners.base.trading_calendar import us_trading_day

    moment = now or datetime.now(timezone.utc)
    session = scan_session.session_at(moment.astimezone(EASTERN))
    trading_day = us_trading_day(moment)
    report = {
        "started_at": moment.isoformat(),
        "trading_day": trading_day,
        "session": session,
        "status": STATUS_OK,
        "exits": [],
        "entry": None,
        "errors": [],
    }

    from state_store.db import open_db

    with open_db() as conn:
        # --- exits first, always, and never gated by entry risk ---------
        try:
            report["exits"] = _run_exits(conn, session=session, now=moment)
        except Exception as exc:  # noqa: BLE001
            logger.error("S2 exit stage failed", exc_info=True)
            report["errors"].append(f"exits: {exc}")

        # --- then at most one entry, through the SHARED cycle -----------
        if not allow_entry:
            report["entry"] = {"skipped": "entry disabled by caller"}
            return report
        try:
            report["entry"] = _run_entry(session=session,
                                         trading_day=trading_day, now=moment)
            if report["entry"].get("status"):
                report["status"] = report["entry"]["status"]
        except Exception as exc:  # noqa: BLE001 - an entry failure must
            # never take the exit stage down with it; the exits above
            # have already run and been recorded.
            logger.error("S2 entry stage failed", exc_info=True)
            report["errors"].append(f"entry: {exc}")
            report["status"] = STATUS_ERROR

    return report


def _run_exits(conn, *, session, now):
    """Evaluate every held S2 position. Runs in every session.

    Orders are only PLACED where the session permits them; elsewhere the
    decision is latched rather than dropped, so a position that should be
    leaving is not forgotten because the clock was wrong.
    """
    from s2_live import exit_runtime, position_store

    if not position_store.load_live(conn):
        return []

    from s2_live import entry_policy

    orders_allowed = session in entry_policy.S2_LIVE_SESSIONS
    broker_adapter, features_fn, price_fn = _runtime_dependencies()
    return exit_runtime.run_exits(
        conn, broker_adapter=broker_adapter, features_fn=features_fn,
        price_fn=price_fn, session=session, now=now,
        orders_allowed=orders_allowed)


def _run_entry(*, session, trading_day, now):
    """Hand S2's source to the shared buy cycle. Places nothing itself."""
    from s2_live import candidate_source as s2_source
    from s2_live import entry_policy

    if session not in entry_policy.S2_LIVE_SESSIONS:
        return {"status": STATUS_SESSION_CLOSED, "session": session,
                "enabled": sorted(entry_policy.S2_LIVE_SESSIONS)}

    import kis_live_trading as klt

    source = s2_source.resolve_for_strategy(
        s2_source.STRATEGY_ID, trading_day=trading_day, session=session)
    described = source.describe()
    if not source.symbols():
        return {"status": STATUS_NO_CANDIDATE, "source": described}

    from brokers.kis_broker import KISBroker

    results = klt.run_live_buy_entry_cycle(
        broker=KISBroker(), now=now, candidate_source=source)
    status = STATUS_OK if results.get("submitted") else STATUS_ENTRY_BLOCKED
    return {"status": status, "source": described, "results": results}


def _runtime_dependencies():
    """Broker adapter and the two observation callables.

    Imported here rather than at module scope so a broker import problem
    surfaces as one failed tick with a reason, not as a script that
    cannot start and therefore never logs anything.
    """
    from brokers.kis_broker import KISBroker
    from live_pilot import armed
    from s1_live import executor as s1_executor

    broker = KISBroker()
    # The SAME adapter construction S1's cron uses. Building one
    # differently here would mean two ideas of what an armed adapter is.
    adapter = armed.build_adapter(broker)
    return (adapter,
            s1_executor.make_features_fn(),
            s1_executor.make_price_fn(broker))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-entry", action="store_true",
                        help="evaluate exits only; place no new entry")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    try:
        report = run_once(allow_entry=not args.no_entry)
    except Exception as exc:  # noqa: BLE001 - a crashed tick must still
        # leave a record; a silent failure is indistinguishable from a
        # tick that never fired.
        logger.error("S2 cycle failed", exc_info=True)
        report = {"status": STATUS_ERROR, "error": str(exc),
                  "started_at": datetime.now(timezone.utc).isoformat()}
        _record(report)
        print(json.dumps(report, default=str))
        return 2

    _record(report)
    print(json.dumps(report, default=str))
    return 0 if report["status"] != STATUS_ERROR else 1


if __name__ == "__main__":
    raise SystemExit(main())
