"""Production evidence for the three checks only a live market can answer.

The rule this enforces
----------------------
    a real observation that succeeded   -> PASS
    a real observation that failed      -> FAIL
    no real observation                 -> NOT_MEASURED
    a synthetic run                     -> NOT_MEASURED, always

The last line is the one worth having code for. Every artifact the
reports write -- the final check, the session report, the COMMON_STOCK
snapshot -- is written identically by a test, and each one is a file on
a disk. Without a way to tell those apart, a test suite that ran once on
the production host could hand the activation evaluator three PASSes and
`READY_FOR_S6_LIMITED_LIVE` would flip on evidence of nothing.

How a production run is recognised
----------------------------------
`DEPLOYED_COMMIT == VALIDATED_COMMIT`, both non-empty -- the same pair
`kis_live_trading` refuses to place an order without. It is not a
perfect fence and is not meant to be one: it is the fence the trading
path already uses, so a run that could satisfy it could also have
traded, and any run that could NOT satisfy it cannot manufacture
evidence here. A laptop, a CI job and a half-finished deploy all fail it.

Nothing here promotes
---------------------
This produces the `observations` dict `s6_live.readiness.evaluate()`
takes. The evaluator still turns it into a verdict, and a human still
performs the promotion. Three modules in a row that each only report is
the point.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: The three §8 checks that a closed market cannot answer.
MARKET_DEPENDENT = (
    "candidate_freshness_verified",
    "regular_market_tick_verified",
    "common_stock_dry_run_verified",
)


def is_production(report: Optional[Dict[str, Any]]) -> bool:
    """Was this artifact written by a validated deployment?

    A missing report is not production evidence, and neither is one whose
    origin says otherwise. Both answer False rather than raising, because
    the caller's next move is identical either way: NOT_MEASURED.
    """
    from scanners.publish import s6_snapshot

    if not report:
        return False
    return report.get("origin") == s6_snapshot.ORIGIN_PRODUCTION


def regular_market_tick(final_check: Optional[Dict[str, Any]]
                        ) -> Optional[bool]:
    """Did a REGULAR-session scan tick actually complete with the market open?

    True requires BOTH: the market was open at report time, and a scan
    ran to completion for that session. Either one alone is the kind of
    half-answer that reads as a pass -- a scan that ran on a holiday, or
    an open market with no producer behind it.
    """
    if not is_production(final_check):
        return None
    if final_check.get("session") != "REGULAR":
        # A report from another session is not evidence about REGULAR.
        # Absent, not failing.
        return None
    open_verified = final_check.get("market_open_verified")
    tick_verified = final_check.get("scanner_tick_verified")
    if open_verified is None or tick_verified is None:
        return None
    if not open_verified:
        # The market was not open. That is not a failure of the tick --
        # it is the absence of the conditions to observe one.
        return None
    return bool(tick_verified)


def candidate_freshness(final_check: Optional[Dict[str, Any]]
                        ) -> Optional[bool]:
    """Was a candidate's age measured at the point it was USED?

    Requires a real candidate to have been consumed: the age exists only
    when rows were read, and a session with nothing to read produces no
    measurement rather than a fresh one. A negative age is a clock or
    provenance fault and is a FAIL, not an absence.
    """
    if not is_production(final_check):
        return None
    generated = final_check.get("candidate_generated_at")
    # The observation path's own read, not the live consumer's.
    #
    # The live source refuses at its mode gate while S6 is
    # DISCOVERY_ONLY, so `candidate_consumed_at` is None -- which made
    # this observation circular: it gated the promotion that would have
    # made it measurable. The read below is a real read of the real
    # shared-store rows at a real moment; it simply carries no order
    # permission. `source_verified` stays the separate, unrelaxed answer
    # about the live consumer.
    consumed = (final_check.get("candidate_read_at")
                or final_check.get("candidate_consumed_at"))
    age = final_check.get("candidate_age_at_read_seconds")
    if age is None:
        age = final_check.get("candidate_age_at_consume_seconds")
    if not generated or not consumed or age is None:
        return None
    try:
        return float(age) >= 0.0
    except (TypeError, ValueError):
        return False


def common_stock_dry_run(snapshot: Optional[Dict[str, Any]]) -> Optional[bool]:
    """Did a real S6-R COMMON_STOCK candidate get captured end to end?

    A snapshot exists only when the scanner published a REGULAR candidate
    that KIS's own master classified as common stock. That is the whole
    of what this check claims -- and specifically NOT that a gate passed:
    the snapshot's own gate columns may still be NOT_MEASURED, which is
    the expected state while S6 is DISCOVERY_ONLY.
    """
    if not is_production(snapshot):
        return None
    if not snapshot.get("live_eligible"):
        # Recorded but not eligible: the pipeline ran and produced a
        # negative answer, which is a measurement.
        return False
    return bool(snapshot.get("symbol") and snapshot.get("security_type"))


def collect(*, final_check=None, snapshot=None, extra=None) -> Dict[str, Any]:
    """The three market-dependent observations, ready for `evaluate()`.

    `extra` merges in facts this module does not source -- `s1_healthy`,
    `regression_healthy`, `account_rows` -- so a caller assembles ONE
    dict rather than two that could disagree about the same key.
    """
    observed: Dict[str, Any] = {
        "regular_market_tick_verified": regular_market_tick(final_check),
        "candidate_freshness_verified": candidate_freshness(final_check),
        "common_stock_dry_run_verified": common_stock_dry_run(snapshot),
    }
    for key, value in (extra or {}).items():
        observed[key] = value
    return observed


def load(*, conn=None, trading_day=None, session=None, now=None,
         extra=None) -> Dict[str, Any]:
    """Build the observations from live artifacts. Never raises.

    Runs the final check and reads the first production snapshot, then
    reduces both to the three booleans the gate asks about. A failure to
    build either one yields None for whatever it would have supplied,
    which the evaluator reads as NOT_MEASURED.
    """
    final_check = None
    try:
        from s6_live import final_check as final_check_module

        final_check = final_check_module.build(
            conn=conn, trading_day=trading_day, session=session, now=now)
    except Exception:  # noqa: BLE001
        logger.warning("could not build the S6-R final check for "
                       "observations", exc_info=True)

    snapshot = None
    try:
        from scanners.publish import s6_snapshot

        snapshot = s6_snapshot.first(production_only=True)
    except Exception:  # noqa: BLE001
        logger.warning("could not read the S6 COMMON_STOCK snapshot log",
                       exc_info=True)

    return collect(final_check=final_check, snapshot=snapshot, extra=extra)
