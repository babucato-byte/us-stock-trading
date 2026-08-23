"""The first S6-R COMMON_STOCK candidate, recorded the moment it exists.

Why a snapshot at all
---------------------
`common_stock_dry_run_verified` is one of the three activation checks
that only a live market can answer, and answering it means showing that a
real S6-R candidate was classified COMMON_STOCK by KIS's own master and
carried every feature the strategy claims to trade on. That evidence
exists for about fifteen minutes: the candidate file is overwritten by
the next cycle, the security master is refreshed, and the intraday bars
behind the features are gone. Reconstructing it later is not possible,
which is the test for what has to be written down at the time.

So this runs on the scanner side, at publication, with no operator in the
loop. An operator who has to remember to capture something will capture
it after the interesting case.

Every one, not only the first
-----------------------------
"The FIRST snapshot" is what the activation gate asks for, but writing
only the first would mean the second interesting candidate -- the one on
the day something looked wrong -- had nothing recorded. The log is
append-only and `first()` names the earliest row in it, so the milestone
is derivable and nothing else is lost.

COMMON_STOCK only, and that is a filter on the RECORD
-----------------------------------------------------
`live_eligible` here comes from the same classification the BUY gate
uses, read through `eligibility.classify_symbol`. A snapshot is not a
permission: the gate in `kis_live_trading` still asks KIS again
immediately before an order, and this file has no path to one.

Gates are NOT_MEASURED here
---------------------------
The BUY-gate columns §3 asks for -- cash, reconciliation, duplicate
protection, risk matrix, execution sanity -- are the trading runtime's
answers, and the scanner runtime is structurally forbidden from asking
them (tests/test_scanner_trading_isolation.py). They are written as
NOT_MEASURED and filled in by `s6_live.final_check`, which runs on the
side that can. A snapshot claiming a gate result it never evaluated
would be the exact failure the activation evaluator exists to prevent,
moved one file over.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SNAPSHOT_FILENAME = "s6_common_stock_snapshots.jsonl"
SCHEMA_VERSION = "s6_common_stock_snapshot_v1"

#: The variant this is evidence for. S6-O candidates are recorded by the
#: session report; the activation gate is about REGULAR.
VARIANT_REGULAR = "S6-R"

NOT_MEASURED = "NOT_MEASURED"

#: The BUY gates §3 lists, in the order the shared cycle applies them.
#: Named here so a reader of a snapshot sees which gates EXIST even when
#: none of them has an answer yet.
BUY_GATES = (
    "instrument",
    "cash_orderability",
    "reconciliation",
    "duplicate_protection",
    "risk_matrix",
    "kis_execution_sanity",
)

#: Set on a snapshot only when the process that wrote it was a validated
#: deployment. See `s6_live.observations` -- a synthetic run must never be
#: able to supply a production PASS.
ORIGIN_PRODUCTION = "PRODUCTION_RUN"
ORIGIN_UNVERIFIED = "UNVERIFIED"


def origin() -> str:
    """Was this written by a deployment that had been validated?

    The same pair `kis_live_trading` refuses to trade without. A test, a
    laptop or a half-finished deploy has them unset or mismatched, so
    nothing it writes can later be read as evidence that the live system
    did something.
    """
    deployed = str(os.environ.get("DEPLOYED_COMMIT") or "").strip()
    validated = str(os.environ.get("VALIDATED_COMMIT") or "").strip()
    if deployed and validated and deployed == validated:
        return ORIGIN_PRODUCTION
    return ORIGIN_UNVERIFIED


def snapshot_path() -> Path:
    from scanners.publish import candidates as publisher

    directory = publisher.candidate_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / SNAPSHOT_FILENAME


def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (
        float("inf"), float("-inf")) else number


def build(row: Dict[str, Any], *, trading_day=None, session=None,
          run_id=None, consumed_at=None, now=None) -> Dict[str, Any]:
    """One enriched candidate row as a snapshot record.

    `row` is a published candidate already through `eligibility.enrich`,
    so the derived measures come from the one implementation of them
    rather than a second copy computed here.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    price = _num(row.get("price"))
    volume = _num(row.get("volume"))
    generated_at = row.get("generated_at")

    return {
        "schema": SCHEMA_VERSION,
        "recorded_at": stamp,
        "origin": origin(),
        "trading_day": str(trading_day or row.get("trading_day") or ""),
        "session": session or row.get("session"),
        "variant": row.get("variant"),
        "scanner_run_id": run_id or row.get("scanner_run_id"),

        "symbol": row.get("symbol"),
        "rank": row.get("rank"),
        "score": _num(row.get("score")),
        "price": price,

        "security_type": row.get("security_type"),
        "etp_type": row.get("etp_type"),
        "exchange": row.get("exchange"),
        "live_eligible": bool(row.get("live_eligible")),
        "security_master_asof": row.get("classified_at"),

        "range_minutes": row.get("range_minutes"),
        "range_high": _num(row.get("range_high")),
        "range_low": _num(row.get("range_low")),
        "range_width_pct": _num(row.get("opening_range_width_pct")),
        "structural_risk_pct": _num(row.get("structural_risk_pct")),

        "breakout_pct": _num(row.get("breakout_pct")),
        "normalized_breakout_by_range": _num(
            row.get("normalized_breakout_by_range")),

        "volume_expansion": _num(row.get("volume_expansion")),
        # The published row carries the raw figures; these are the names
        # §3 asks for, mapped rather than recomputed.
        "daily_relative_volume": _num(row.get("volume_multiple")),
        "absolute_volume": volume,
        "dollar_volume": (price * volume
                          if price is not None and volume is not None else None),

        "vwap": _num(row.get("vwap")),
        "vwap_distance_pct": _num(row.get("vwap_distance_pct")),
        "ema9": _num(row.get("ema9")),
        "ema21": _num(row.get("ema21")),
        "ema_spread_pct": _num(row.get("ema_spread_pct")),

        "generated_at": generated_at,
        # Absent at publication: nothing has consumed the row yet. Filled
        # in by the final-check report, which is the thing that consumes
        # it. Recording "now" here would report the age of the write.
        "consumed_at": consumed_at,
        "candidate_age_seconds": _age(generated_at, consumed_at),

        # Answered on the trading side. See the module docstring.
        "qualify_result": NOT_MEASURED,
        "buy_gates": {gate: NOT_MEASURED for gate in BUY_GATES},
        "broker_submit_count": 0,
    }


def _age(generated_at, consumed_at) -> Optional[float]:
    if not generated_at or not consumed_at:
        return None
    try:
        from s1_live.freshness import as_utc
    except Exception:  # noqa: BLE001
        return None
    made, used = as_utc(generated_at), as_utc(consumed_at)
    if made is None or used is None:
        return None
    return round((used - made).total_seconds(), 3)


def record_from_published(rows: List[Dict[str, Any]], *, trading_day=None,
                          session=None, run_id=None, index=None,
                          now=None) -> List[Dict[str, Any]]:
    """Snapshot every S6-R COMMON_STOCK row in a freshly published set.

    Returns what it wrote, which is usually nothing: most cycles produce
    no candidate, and most candidates are not common stock.
    """
    from scanners.publish import eligibility

    wanted = [r for r in (rows or [])
              if str(r.get("variant") or "") == VARIANT_REGULAR]
    if not wanted:
        return []

    enriched = eligibility.enrich(wanted, index=index)
    eligible = [r for r in enriched if r.get("live_eligible")]
    if not eligible:
        return []

    written = []
    for row in eligible:
        record = build(row, trading_day=trading_day, session=session,
                       run_id=run_id, now=now)
        if append(record):
            written.append(record)
    if written:
        logger.info("recorded %d S6-R COMMON_STOCK snapshot(s): %s",
                    len(written), ", ".join(str(r["symbol"]) for r in written))
    return written


def append(record: Dict[str, Any]) -> bool:
    """Write one record. Never raises -- see `candidates.publish`."""
    try:
        with snapshot_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        return True
    except Exception:  # noqa: BLE001
        logger.warning("could not write an S6 candidate snapshot",
                       exc_info=True)
        return False


def read(*, trading_day=None, variant=None) -> List[Dict[str, Any]]:
    """Every snapshot, oldest first. Missing file -> []."""
    try:
        path = snapshot_path()
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                logger.warning("skipping an unparseable S6 snapshot row")
    except Exception:  # noqa: BLE001
        logger.warning("could not read the S6 snapshot log", exc_info=True)
        return []

    if trading_day is not None:
        rows = [r for r in rows if str(r.get("trading_day")) == str(trading_day)]
    if variant is not None:
        rows = [r for r in rows if str(r.get("variant")) == str(variant)]
    return rows


def first(*, variant=VARIANT_REGULAR, production_only=False
          ) -> Optional[Dict[str, Any]]:
    """The earliest recorded snapshot -- the milestone the gate asks for.

    `production_only` narrows it to records a validated deployment wrote.
    The activation evaluator uses that form: a snapshot produced by a
    test run is a snapshot of a test, and it is not evidence that the
    live pipeline ever classified a real candidate.
    """
    rows = read(variant=variant)
    if production_only:
        rows = [r for r in rows if r.get("origin") == ORIGIN_PRODUCTION]
    return rows[0] if rows else None
