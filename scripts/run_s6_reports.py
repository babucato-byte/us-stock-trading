#!/usr/bin/env python3
"""The four S6 reports, and the activation gate they feed. Read-only.

    scripts/run_s6_reports.py                 # all four, plus the gate
    scripts/run_s6_reports.py --report final-check
    scripts/run_s6_reports.py --report session
    scripts/run_s6_reports.py --report trade
    scripts/run_s6_reports.py --report readiness
    scripts/run_s6_reports.py --json

One entry point rather than four, because these are read together: the
final check says whether REGULAR is working, the session report says the
same for a shadow session, the trade timeline describes the first real
trade, and the readiness gate is the verdict all of them feed. Four
scripts would mean four ways to be looking at a different trading day
than the one you meant.

It places no order and promotes nothing
---------------------------------------
No broker connection is opened unless `--live` is given, and even then it
is used only for reads -- account, positions, open orders. Nothing here
writes `scanner_live_mode`, `LIVE_SESSIONS` or any rollout flag, and
`broker_submit_count` is 0 on every path.

Exit codes
    0  the report rendered (whatever it says)
    1  it could not be built
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPORTS = ("final-check", "session", "trade", "readiness", "variants", "all")


def _broker(live: bool):
    """A read-only broker, or None.

    Constructed only when asked for. The reports treat None as
    NOT_MEASURED for every gate that needs an account, which is the
    correct answer when nobody looked.
    """
    if not live:
        return None
    from brokers.kis_broker import KISBroker

    return KISBroker()


def _attested(value):
    """An operator-supplied fact: True, False, or unstated.

    Unstated is None, and the evaluator reads None as NOT_MEASURED. The
    three-way answer is the whole point -- `--s1-healthy` absent must not
    mean "S1 is unhealthy", and it must not mean "S1 is fine" either.
    """
    if value is None:
        return None
    return str(value).strip().lower() in ("1", "true", "yes", "ok", "pass")


def _account_rows(broker):
    """Positions as reconciliation wants them, or None.

    None rather than [] on a failed read: an empty account and an
    unreadable one are opposite facts, and `evaluate` reports the second
    as NOT_MEASURED only if it is given nothing.
    """
    if broker is None:
        return None
    try:
        return [{"symbol": p.symbol, "venue": getattr(p, "venue", None),
                 "quantity": p.quantity}
                for p in (broker.get_positions() or [])]
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "could not read positions for the reconciliation check",
            exc_info=True)
        return None


def _readiness(conn, *, trading_day, session, now, crontab, broker=None,
               s1_healthy=None, regression_healthy=None):
    from s6_live import observations, readiness

    extra = {"s1_healthy": s1_healthy,
             "regression_healthy": regression_healthy,
             "account_rows": _account_rows(broker)}
    observed = observations.load(conn=conn, trading_day=trading_day,
                                 session=session, now=now, extra=extra)
    return readiness.evaluate(conn=conn, crontab=crontab,
                              observations=observed)


def _crontab(path=None):
    """The live crontab, or None if it cannot be read.

    None is deliberate: `readiness.evaluate` reports NOT_MEASURED for the
    scheduler checks rather than FAIL when no crontab was supplied, and a
    machine where `crontab -l` is unavailable has not proved the timers
    are missing. Returning "" instead would make every such machine
    report the scheduler as absent.
    """
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return None

    import subprocess

    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True,
                                text=True, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    return result.stdout if result.returncode == 0 else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", choices=REPORTS, default="all")
    parser.add_argument("--trading-day", default=None)
    parser.add_argument("--session", default=None,
                        help="PREMARKET/REGULAR/AFTER_HOURS/OVERNIGHT_DAYTIME; "
                             "defaults to the ET clock")
    parser.add_argument("--position-id", default=None,
                        help="timeline for one position; defaults to the "
                             "first S6 trade ever recorded")
    parser.add_argument("--live", action="store_true",
                        help="open a READ-ONLY broker connection so the "
                             "account-dependent gates can be measured")
    parser.add_argument("--attach-snapshots", action="store_true",
                        help="write the final check's per-candidate gate "
                             "answers onto the COMMON_STOCK snapshot log")
    parser.add_argument("--crontab-file", default=None,
                        help="read the crontab from a file instead of "
                             "`crontab -l` (for a host where cron is "
                             "installed under a different user)")
    parser.add_argument("--s1-healthy", default=None,
                        help="operator attestation: is S1 healthy right now? "
                             "omitted means NOT_MEASURED, never PASS")
    parser.add_argument("--regression-healthy", default=None,
                        help="operator attestation: did the full regression "
                             "pass on the deployed commit? omitted means "
                             "NOT_MEASURED, never PASS")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    from state_store import db

    payload = {}
    conn = db.open_db()
    try:
        broker = _broker(args.live)
        want = args.report

        if want in ("final-check", "all"):
            from s6_live import final_check

            report = final_check.build(conn=conn, broker=broker,
                                       trading_day=args.trading_day,
                                       session=args.session)
            payload["final_check"] = report
            if args.attach_snapshots:
                payload["snapshots_amended"] = final_check.attach_to_snapshots(
                    report)
            if not args.json:
                print(final_check.format_report(report))
                print("")

        if want in ("session", "all"):
            from s6_live import session_report

            report = session_report.build(conn=conn,
                                          trading_day=args.trading_day,
                                          session=args.session)
            payload["session_report"] = report
            if not args.json:
                print(session_report.format_report(report))
                print("")

        if want in ("trade", "all"):
            from s6_live import trade_timeline

            report = trade_timeline.build(conn, position_id=args.position_id)
            payload["trade_timeline"] = report
            if not args.json:
                print(trade_timeline.format_report(report))
                print("")

        if want in ("variants", "all"):
            from s6_live import observations as obs_module
            from s6_live import variant_state

            # Observations come from the production artifacts, exactly as
            # the activation gate takes them. A variant's state can only
            # move on evidence a validated deployment recorded.
            observed = obs_module.load(conn=conn, trading_day=args.trading_day,
                                       session=args.session)
            states = variant_state.evaluate(observations=observed)
            payload["variants"] = {k: v.as_dict() for k, v in states.items()}
            if not args.json:
                print("S6 VARIANT STATES")
                print("=" * 64)
                print(variant_state.format_table(states))
                print("")
                for state in states.values():
                    for key, why in sorted(state.detail.items()):
                        print(f"  {key}: {why}")
                print("")

        if want in ("readiness", "all"):
            verdict = _readiness(
                conn, trading_day=args.trading_day, session=args.session,
                now=None, crontab=_crontab(args.crontab_file), broker=broker,
                s1_healthy=_attested(args.s1_healthy),
                regression_healthy=_attested(args.regression_healthy))
            payload["readiness"] = verdict.as_dict()
            if not args.json:
                print(_format_readiness(verdict))
    finally:
        conn.close()

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def _format_readiness(verdict) -> str:
    from s6_live import readiness

    lines = ["S6 ACTIVATION GATE", "=" * 64]
    counts = {readiness.PASS: 0, readiness.FAIL: 0, readiness.NOT_MEASURED: 0}
    for name in readiness.CHECKS:
        result = verdict.checks[name]
        counts[result.status] = counts.get(result.status, 0) + 1
        marker = "  " if result.status == readiness.PASS else "!!"
        market = " (market-dependent)" if name in readiness.MARKET_DEPENDENT else ""
        lines.append(f"  {marker} {name:<32}: {result.status:<13} "
                     f"{result.detail}{market}")
    lines += [
        "",
        f"  PASS {counts.get(readiness.PASS, 0)}  "
        f"FAIL {counts.get(readiness.FAIL, 0)}  "
        f"NOT_MEASURED {counts.get(readiness.NOT_MEASURED, 0)}",
        f"  verdict                       : {verdict.verdict}",
        f"  READY_FOR_S6_LIMITED_LIVE     : {verdict.ready}",
        "",
        "  This report cannot promote S6. Changing scanner_live_mode is a",
        "  separate, deliberate act performed after this verdict is read.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
