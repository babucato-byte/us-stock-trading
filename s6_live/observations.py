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


#: Session -> the observation-name prefix that session's evidence uses.
#: Kept identical to `variant_state.PREFIX` so a supplier and a consumer
#: of the same fact cannot disagree about its name.
SESSION_PREFIX = {
    "OVERNIGHT_DAYTIME": "overnight",
    "PREMARKET": "premarket",
    "REGULAR": "regular",
    "AFTER_HOURS": "afterhours",
}


def market_tick_for_session(session, final_check: Optional[Dict[str, Any]]
                            ) -> Optional[bool]:
    """Did a real scan tick complete in THIS session? One rule, four sessions.

    The conditions are the same for every variant and none of them is
    "the REGULAR market is open" -- that is CLOSED for three of the four
    sessions and using it would make three variants permanently
    unobservable. What is required is that the calendar allowed a scan,
    the scan actually ran to completion, and the publisher wrote it:

        origin == PRODUCTION_RUN        (validated deployment)
        calendar_trading_day           not a weekend, not a holiday
        scan_allowed                   this session was scannable
        scan_ran                       a producer actually ran
        scan_in_progress is False      it finished
        last scan status == OK         and finished cleanly
        publisher_verified             and the hand-off was written
        session matches                evidence is never shared

    REGULAR carries ONE extra condition, because REGULAR is the session
    whose own market state is meaningful: `market_open_verified`. A
    closed REGULAR market yields None -- the absence of the conditions to
    observe a tick, not a failure of one.

    Returns None (NOT_MEASURED) when the window did not exist or the
    evidence is missing, and False (FAIL) only when a scan genuinely
    failed.
    """
    from scanners.base import scan_session

    wanted = scan_session.normalize(session)
    if wanted is None or not is_production(final_check):
        return None

    # Evidence is per session and is never borrowed. An OVERNIGHT tick
    # says nothing about REGULAR.
    if final_check.get("session") != wanted:
        return None

    if final_check.get("calendar_trading_day") is not True:
        return None                                   # weekend / holiday
    if final_check.get("scan_allowed") is not True:
        return None                                   # window did not exist

    status = final_check.get("last_scan_status")
    if status is not None and str(status) != "OK":
        return False                                  # the scan FAILED

    if not final_check.get("scan_ran"):
        return None                                   # producer never ran
    if final_check.get("scan_in_progress"):
        return None                                   # not finished yet
    if not final_check.get("publisher_verified"):
        return None

    if wanted == "REGULAR":
        # The one session whose own market state is the right question.
        if not final_check.get("market_open_verified"):
            return None

    return bool(final_check.get("scanner_tick_verified"))


def regular_market_tick(final_check: Optional[Dict[str, Any]]
                        ) -> Optional[bool]:
    """REGULAR's tick. Kept as a named entry point; one implementation."""
    return market_tick_for_session("REGULAR", final_check)


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


def candidate_freshness_for_session(session, final_check
                                    ) -> Optional[bool]:
    """This session's freshness. Same rule, never shared across sessions."""
    from scanners.base import scan_session

    wanted = scan_session.normalize(session)
    if wanted is None or not is_production(final_check):
        return None
    if final_check.get("session") != wanted:
        return None
    return candidate_freshness(final_check)


def common_stock_dry_run_for_session(session, final_check
                                     ) -> Optional[bool]:
    """Did a real COMMON_STOCK candidate clear every CANDIDATE gate?

    Reads `common_stock_candidate_dry_run`, which deliberately excludes
    the session's order policy -- a shadow session can fully observe a
    candidate while still refusing to trade it. `order_policy_ready` is
    the separate answer and is what actually gates a promotion.
    """
    from scanners.base import scan_session

    wanted = scan_session.normalize(session)
    if wanted is None or not is_production(final_check):
        return None
    if final_check.get("session") != wanted:
        return None
    verdict = (final_check.get("common_stock_candidate_dry_run") or {}).get(
        "status")
    if verdict == "PASS":
        return True
    return None


def for_session(session, final_check=None, snapshot=None) -> Dict[str, Any]:
    """Every observation this session can supply, under its own names."""
    from scanners.base import scan_session

    wanted = scan_session.normalize(session)
    prefix = SESSION_PREFIX.get(wanted or "")
    if prefix is None:
        return {}
    out = {
        f"{prefix}_market_tick_verified":
            market_tick_for_session(wanted, final_check),
        f"{prefix}_candidate_freshness_verified":
            candidate_freshness_for_session(wanted, final_check),
        f"{prefix}_common_stock_dry_run_verified":
            common_stock_dry_run_for_session(wanted, final_check),
    }
    if wanted == "REGULAR":
        # The activation gate's own three names are REGULAR's.
        out["regular_market_tick_verified"] = out[
            "regular_market_tick_verified"]
        out["candidate_freshness_verified"] = out[
            "regular_candidate_freshness_verified"]
        out["common_stock_dry_run_verified"] = (
            common_stock_dry_run(snapshot)
            if snapshot is not None else
            out["regular_common_stock_dry_run_verified"])
    return out


def collect(*, final_check=None, snapshot=None, extra=None) -> Dict[str, Any]:
    """The three market-dependent observations, ready for `evaluate()`.

    `extra` merges in facts this module does not source -- `s1_healthy`,
    `regression_healthy`, `account_rows` -- so a caller assembles ONE
    dict rather than two that could disagree about the same key.
    """
    observed: Dict[str, Any] = {
        "regular_market_tick_verified": regular_market_tick(final_check),
        "candidate_freshness_verified": candidate_freshness_for_session(
            "REGULAR", final_check),
        "common_stock_dry_run_verified": common_stock_dry_run(snapshot),
    }
    # Whatever session the report actually came from also supplies its
    # own prefixed observations, so a variant table can show the evidence
    # that session produced without any of it leaking into another's.
    if final_check is not None:
        observed.update(for_session(final_check.get("session"), final_check,
                                    snapshot))
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
