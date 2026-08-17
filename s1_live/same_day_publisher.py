"""Publish a same-day S1 scan into the candidate store, common stocks only.

This is the join that was missing. `same_day_scan` computes S1 from
completed bars and `S1CandidateSource` reads the candidate store, but
nothing connected the two -- so the live path kept refusing with "no
candidate file" no matter what the scan found.

Two filters are applied here and nowhere else:

  security type   KIS's own master must call the symbol a common stock on
                  a supported exchange. ETP, INDEX, WARRANT and anything
                  absent from the master are dropped. See
                  `s1_live/security_type.py` for why the master rather
                  than a name rule or a per-symbol network call.

  ranking         is NOT recomputed. The scan's own order -- score
                  descending, symbol ascending on ties -- is preserved and
                  simply renumbered after the drops, so removing an ETF
                  promotes the next name rather than reshuffling anything.

What this module must not do
---------------------------
It does not touch S1's conditions, weights or score. A symbol's
eligibility to be BOUGHT is a different question from whether it is a
signal, and mixing the two would mean the strategy being measured is not
the strategy being reported. Dropped symbols are recorded with a reason
rather than silently vanishing, so "S1 found nothing" and "S1 found only
ETFs" stay distinguishable.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from s1_live import security_type as sectype
from s1_live import store

logger = logging.getLogger(__name__)

#: Stage 1 holds one position and makes one entry a day, so a long file
#: buys nothing. Kept at the store's own ceiling rather than a new number.
MAX_PUBLISHED_CANDIDATES = 10

SCANNER_NAME = "hma_early_trend"


class SameDayPublishRefused(Exception):
    """Nothing was written. The live path keeps refusing, which is the
    safe direction."""


@dataclass
class PublishOutcome:
    trading_day: str
    signal_day: str
    published: List[Dict[str, Any]] = field(default_factory=list)
    dropped: List[Dict[str, Any]] = field(default_factory=list)
    manifest: Optional[Dict[str, Any]] = None

    @property
    def count(self) -> int:
        return len(self.published)

    def drop_reasons(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in self.dropped:
            counts[row["reason"]] = counts.get(row["reason"], 0) + 1
        return counts

    def as_dict(self) -> Dict[str, Any]:
        return {
            "trading_day": self.trading_day, "signal_day": self.signal_day,
            "published_count": self.count,
            "published": [r["symbol"] for r in self.published],
            "dropped_count": len(self.dropped),
            "drop_reasons": self.drop_reasons(),
            "scanner_run_id": (self.manifest or {}).get("scanner_run_id"),
        }


def eligible_candidates(candidates, *, index=None):
    """Split a scan's candidates into (kept, dropped-with-reason).

    Order is preserved. `index` is passed in so one loaded master serves
    the whole batch -- classification is a local join, never a per-symbol
    call.
    """
    idx = index or sectype.load_index()
    kept, dropped = [], []
    for candidate in candidates:
        verdict = idx.classify(candidate.symbol)
        reason = verdict.ineligible_reason()
        if reason is None:
            kept.append((candidate, verdict))
        else:
            dropped.append({
                "symbol": candidate.symbol, "reason": reason,
                "security_type": verdict.security_type,
                "etp_type": verdict.etp_type,
                "score": candidate.score,
            })
    return kept, dropped


def publish_scan(scan, *, index=None, limit: int = MAX_PUBLISHED_CANDIDATES,
                 market_data_provider: str, config_fingerprint: str,
                 scanner_version: str, scanner_run_id: Optional[str] = None,
                 generated_at=None) -> PublishOutcome:
    """Write the eligible slice of `scan` to the candidate store.

    Refuses -- writing nothing -- when the scan could not be performed.
    An unanswerable scan is not an empty one, and publishing an empty file
    for one would look exactly like "the market offered nothing today".
    """
    from s1_live.same_day_scan import STATUS_DATA_UNAVAILABLE

    if scan is None:
        raise SameDayPublishRefused("no scan result")
    if scan.status == STATUS_DATA_UNAVAILABLE:
        raise SameDayPublishRefused(
            f"scan status is {scan.status} -- refusing to publish a candidate "
            f"file that would be indistinguishable from a genuine zero "
            f"(evaluated={scan.evaluated} unavailable={scan.unavailable})")

    idx = index or sectype.load_index()
    kept, dropped = eligible_candidates(scan.candidates, index=idx)
    if dropped:
        logger.info("S1 same-day publish dropped %d of %d candidates: %s",
                    len(dropped), len(scan.candidates),
                    {r["reason"]: sum(1 for d in dropped if d["reason"] == r["reason"])
                     for r in dropped})

    stamp = generated_at or datetime.now(timezone.utc)
    run_id = scanner_run_id or f"s1sameday_{uuid.uuid4().hex[:16]}"
    signal_ts = stamp.astimezone(timezone.utc).isoformat()

    rows = []
    for rank, (candidate, _verdict) in enumerate(kept[:limit], start=1):
        rows.append({
            "rank": rank,
            "symbol": candidate.symbol,
            "scanner_score": candidate.score,
            "signal_price": candidate.signal_price,
            # The signal identity carries the SIGNAL day, not the trading
            # day: two sessions re-scanning the same completed bars are
            # looking at the same signal, and the re-entry guard should
            # see that rather than treating each session as new.
            "signal_id": f"s1-{candidate.symbol}-{candidate.signal_day}",
            "signal_timestamp": signal_ts,
            "scanner_run_id": run_id,
            "trading_day": scan.trading_day,
        })

    outcome = PublishOutcome(trading_day=scan.trading_day, signal_day=scan.signal_day,
                             dropped=dropped)
    if not rows:
        # Nothing eligible. The store is deliberately NOT written: leaving
        # yesterday's file in place would be worse, and writing an empty
        # one adds nothing the caller does not already know.
        logger.info("S1 same-day publish: no eligible candidates "
                    "(%d scanned candidates, all dropped)", len(scan.candidates))
        return outcome

    outcome.manifest = store.publish(
        rows, trading_day=scan.trading_day, source_scanner=SCANNER_NAME,
        scanner_version=scanner_version, scanner_run_id=run_id,
        config_fingerprint=config_fingerprint,
        market_data_provider=market_data_provider, generated_at=stamp)
    outcome.published = rows
    logger.info("S1 same-day publish: %d eligible candidates for %s "
                "(signal_day %s, run %s)", len(rows), scan.trading_day,
                scan.signal_day, run_id)
    return outcome
