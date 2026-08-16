"""Turn one day's S1 signals into a published candidate set.

    logs/scanners/signals/<day>.jsonl   (read only)
              |  scanner_name == the single LIMITED_LIVE scanner
              v
    shared/state/s1_live_candidates.csv + .manifest.json

What this module deliberately does NOT do
-----------------------------------------
It does not re-implement, re-score or re-filter the scanner. No price
floor, no dollar-volume floor, no market cap, no sector, no extra
technical threshold -- adding any of those here would be a second,
undocumented strategy sitting between the one that was measured for a
month and the order path, and the month-1 record would no longer
describe what actually trades.

Instrument-level exclusions (leveraged, inverse, untradable) are not
here either, because they are already guaranteed downstream by
`domain/instrument.py` and the broker's own gate. Duplicating them would
create two places that could disagree about whether a symbol is
tradable.

The only reduction applied is `MAX_S1_LIVE_CANDIDATES`: a cap on how
many symbols are exposed to further checking. It is not a cap on orders
(the rollout's own position and daily-entry limits are), and it is not a
quality judgement -- the ordering it truncates is the scanner's own
score, unmodified.

Provenance is checked, not assumed
----------------------------------
A candidate set is only published when a run manifest for that trading
day shows the S1 scanner actually completing successfully. The published
manifest then carries that run's id, config fingerprint and data
provider, so the consumer can require the same values back. A signal
file with no corresponding successful run is refused: signals can exist
from a partially-completed or superseded run, and a candidate set built
from those would claim provenance it does not have.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import scanner_live_mode
from scanners.base import result_store, run_context
from s1_live import store

logger = logging.getLogger(__name__)

#: How many symbols reach the downstream checks. NOT a maximum order
#: count -- `LIVE_ROLLOUT_MAX_POSITIONS` and `LIVE_ROLLOUT_MAX_DAILY_ENTRIES`
#: are, and they are both 1.
MAX_S1_LIVE_CANDIDATES = 10


class S1PublishRefused(Exception):
    """The candidate set was not published, with the reason. Refusing is
    a normal outcome (a quiet day, a failed scan) and callers report it
    rather than treating it as a crash."""


def _signal_rows(trading_day: str, scanner_name: str) -> List[Dict[str, Any]]:
    return [row for row in result_store.read_signal_rows(trading_day)
            if str(row.get("scanner_name")) == scanner_name]


def latest_successful_run(trading_day: str, scanner_name: str) -> Optional[Dict[str, Any]]:
    """The most recent run manifest in which `scanner_name` SUCCEEDED.

    Latest wins because a re-run supersedes an earlier one; a run id is
    per-invocation precisely so the two stay distinguishable. Manifests
    are appended in order, so the last matching entry is the newest.
    """
    found = None
    for manifest in result_store.read_run_manifests(trading_day):
        if manifest.get("run_status") not in (run_context.SUCCESS, run_context.PARTIAL):
            continue
        for entry in manifest.get("scanners") or []:
            if (str(entry.get("scanner_name")) == scanner_name
                    and entry.get("status") == run_context.SUCCESS
                    and not entry.get("failed")):
                found = (manifest, entry)
    return found


def rank_signals(rows: List[Dict[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
    """Descending scanner_score, ties broken by symbol ascending.

    The score is the scanner's own, used only for ordering. The symbol
    tiebreak is what makes the output reproducible: without it, two
    equally-scored symbols would order by however the store yielded
    them, and the same day's file would differ between two runs.
    """
    def sort_key(row):
        score = row.get("scanner_score")
        try:
            value = float(score)
        except (TypeError, ValueError):
            value = float("-inf")
        return (-value, str(row.get("symbol") or "").upper())

    return sorted(rows, key=sort_key)[:max(0, int(limit))]


def build(trading_day: str, *, limit: int = MAX_S1_LIVE_CANDIDATES,
          modes=None) -> Dict[str, Any]:
    """The rows and manifest fields for `trading_day`, or raise S1PublishRefused."""
    try:
        scanner_name = scanner_live_mode.limited_live_scanner(modes)
    except scanner_live_mode.ScannerLiveModeError as exc:
        raise S1PublishRefused(f"live-mode configuration refused: {exc}") from exc

    found = latest_successful_run(trading_day, scanner_name)
    if found is None:
        raise S1PublishRefused(
            f"no successful {scanner_name} run recorded for {trading_day}; "
            "refusing to publish candidates with no provenance")
    manifest, entry = found

    rows = _signal_rows(trading_day, scanner_name)
    run_id = str(manifest.get("run_id") or "")
    # Only signals from THAT run. A signal left over from a superseded
    # run of the same day carries a different id, and mixing the two
    # would publish a set no single snapshot ever produced.
    rows = [row for row in rows if str(row.get("scanner_run_id") or "") == run_id]

    ranked = rank_signals(rows, limit=limit)
    csv_rows = [{
        "rank": position,
        "symbol": str(row.get("symbol") or "").upper(),
        "scanner_score": row.get("scanner_score"),
        "signal_price": row.get("signal_price"),
        "signal_id": row.get("signal_id"),
        "scanner_run_id": run_id,
        "trading_day": trading_day,
    } for position, row in enumerate(ranked, start=1)]

    return {
        "rows": csv_rows,
        "trading_day": trading_day,
        "source_scanner": scanner_name,
        "scanner_version": str(entry.get("scanner_version") or ""),
        "scanner_run_id": run_id,
        "config_fingerprint": str(entry.get("config_fingerprint") or ""),
        "market_data_provider": str(manifest.get("market_data_provider")
                                    or manifest.get("provider") or ""),
        "signals_seen": len(rows),
        "truncated": max(0, len(rows) - len(csv_rows)),
    }


def publish(trading_day: str, *, limit: int = MAX_S1_LIVE_CANDIDATES,
            modes=None, generated_at=None) -> Dict[str, Any]:
    """Build and atomically publish. Returns a summary for the caller to print."""
    built = build(trading_day, limit=limit, modes=modes)
    manifest = store.publish(
        built["rows"],
        trading_day=built["trading_day"],
        source_scanner=built["source_scanner"],
        scanner_version=built["scanner_version"],
        scanner_run_id=built["scanner_run_id"],
        config_fingerprint=built["config_fingerprint"],
        market_data_provider=built["market_data_provider"],
        generated_at=generated_at or datetime.now(timezone.utc),
    )
    logger.info("published %s S1 candidates for %s (run %s, %s signals seen, %s truncated)",
                len(built["rows"]), trading_day, built["scanner_run_id"],
                built["signals_seen"], built["truncated"])
    return {
        "manifest": manifest,
        "rows": built["rows"],
        "signals_seen": built["signals_seen"],
        "truncated": built["truncated"],
        "candidate_path": str(store.candidate_path()),
        "manifest_path": str(store.manifest_path()),
    }
