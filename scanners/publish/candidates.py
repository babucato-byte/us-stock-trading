"""Scanner-side candidate publication.

The boundary this file sits on
------------------------------
The scanner runtime observes; the trading runtime orders. This module is
the only thing that crosses between them, and it crosses in one
direction: it WRITES a record of what a scan found. It imports no broker,
holds no account, and has no code path to an order -- deliberately, and
`tests/test_candidate_publisher.py` asserts it against the import graph
rather than against this paragraph.

Not `candidate_decision.publish()`
----------------------------------
That function exists and always raises: Candidate Decision is disabled,
and it is the gate that would let a scanner's output become a trade
automatically. Nothing here re-opens it. What this writes is an
observation record -- "S2 ranked these five at 15:45 REGULAR" -- which a
consumer may read, ignore, or re-filter. The distinction is that
publishing a candidate is not selecting one, and the trading runtime
still applies its own entry policy, its own risk limits and its own
execution-time confirmation before anything is bought.

Why the record carries provenance
---------------------------------
A candidate without its provenance cannot be checked later. Six weeks on,
"why was ABC rank 2" is answerable only if the row still knows which
scanner version, which config fingerprint, which run and which session
produced it. Those fields cost nothing to write and cannot be
reconstructed afterwards, which is the test for what belongs in a record
at all.

Ranks are positions in THIS run
-------------------------------
`rank` is assigned from the run's own ordering (score descending, symbol
as the tie-break, matching what the monitor prints). It is not a score
threshold and not a selection: rank 1 of a weak day is still a weak
candidate, and nothing downstream should read rank as a recommendation.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: Where published candidates land. Separate from both the analytics
#: store (`SCANNER_ANALYTICS_DIR`, which holds every signal) and the KIS
#: candidate directory: this is the hand-off record, and mixing it into
#: either would make "what did we publish" a query rather than a file.
CANDIDATE_DIR_ENV = "SCANNER_CANDIDATE_DIR"
DEFAULT_SUBDIR = "candidates"

SCHEMA_VERSION = "scanner_candidates_v1"


@dataclass(frozen=True)
class PublishedCandidate:
    """One row. Every field is either observed or recorded, never derived
    at read time -- a consumer must not have to recompute anything to know
    what the scanner saw."""

    strategy_id: str
    scanner_run_id: Optional[str]
    trading_day: str
    session: Optional[str]
    generated_at: str
    symbol: str
    rank: int
    score: Optional[float]
    price: Optional[float]
    volume: Optional[float] = None
    avg_volume: Optional[float] = None
    volume_multiple: Optional[float] = None
    price_change_pct: Optional[float] = None
    hma200: Optional[float] = None
    hma200_slope: Optional[float] = None
    hma89: Optional[float] = None
    vwap: Optional[float] = None
    #: Everything needed to reproduce the judgement later.
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def candidate_dir() -> Path:
    configured = os.environ.get(CANDIDATE_DIR_ENV)
    if configured and str(configured).strip():
        return Path(str(configured).strip())
    from scanners.base import result_store

    return result_store.analytics_dir() / DEFAULT_SUBDIR


def candidates_path(trading_day: str, session: Optional[str] = None) -> Path:
    """One file per (day, session).

    Per session, not per day: the whole point of a session-aware scan is
    that the same day produces different answers at different hours, and
    appending them to one file would leave a reader unable to tell a
    re-scan from a second observation without parsing every row.
    """
    directory = candidate_dir()
    directory.mkdir(parents=True, exist_ok=True)
    suffix = f"-{session}" if session else ""
    return directory / f"{trading_day}{suffix}.jsonl"


def _number(value):
    """A float, or None. NaN and inf become None rather than travelling.

    A NaN that reaches a JSON file is not valid JSON, and a NaN that
    reaches a comparison silently answers False to every question asked
    of it -- including the ones a risk check asks.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def build_rows(signals: Iterable[Any], *, strategy_id: str, trading_day: str,
               session: Optional[str], run_id: Optional[str] = None,
               generated_at: Optional[str] = None) -> List[PublishedCandidate]:
    """Rank a scan's signals and turn them into publishable rows.

    Ranking is score-descending with the symbol as tie-break -- the same
    order the monitor prints, so a reader comparing the channel with the
    file never sees two different "rank 1"s for one run.
    """
    stamp = generated_at or datetime.now(timezone.utc).isoformat()
    ordered = sorted(
        list(signals or []),
        key=lambda sig: (-(_number(getattr(sig, "scanner_score", None)) or 0.0),
                         str(getattr(sig, "symbol", ""))))

    rows: List[PublishedCandidate] = []
    for position, signal in enumerate(ordered, start=1):
        metrics = getattr(signal, "metrics", None) or {}
        rows.append(PublishedCandidate(
            strategy_id=strategy_id,
            scanner_run_id=run_id or getattr(signal, "scanner_run_id", None),
            trading_day=trading_day,
            session=session,
            generated_at=stamp,
            symbol=str(getattr(signal, "symbol", "")),
            rank=position,
            score=_number(getattr(signal, "scanner_score", None)),
            price=_number(getattr(signal, "signal_price", None)),
            volume=_number(getattr(signal, "volume", None)),
            avg_volume=_number(getattr(signal, "avg_volume", None)),
            volume_multiple=_number(getattr(signal, "volume_multiple", None)),
            price_change_pct=_number(getattr(signal, "price_change_pct", None)),
            hma200=_number(getattr(signal, "hma200", None)),
            hma200_slope=_number(getattr(signal, "hma200_slope", None)),
            hma89=_number(getattr(signal, "hma89", None)),
            vwap=_number(getattr(signal, "vwap", None)),
            provenance={
                "schema": SCHEMA_VERSION,
                "scanner_name": getattr(signal, "scanner_name", None),
                "scanner_version": getattr(signal, "scanner_version", None),
                "signal_id": getattr(signal, "signal_id", None),
                "config_fingerprint": metrics.get("config_fingerprint"),
                "market_data_provider": getattr(signal, "market_data_provider", None),
                "market_data_feed": getattr(signal, "market_data_feed", None),
                "data_timestamp": getattr(signal, "data_timestamp", None),
                "feature_timestamp": getattr(signal, "feature_timestamp", None),
                "source_timeframe": getattr(signal, "source_timeframe", None),
                "signal_timestamp": getattr(signal, "timestamp", None),
                "reasons": list(getattr(signal, "reasons", None) or []),
                # Stated on every row so a consumer never has to look
                # elsewhere to learn that publication is not selection.
                "candidate_decision": "DISABLED",
                "published_by": "scanner_runtime",
            },
        ))
    return rows


def publish(signals: Iterable[Any], *, strategy_id: str, trading_day: str,
            session: Optional[str], run_id: Optional[str] = None,
            generated_at: Optional[str] = None) -> List[PublishedCandidate]:
    """Write the rows and return them. Never raises.

    A publication failure must not fail a scan: the scan already happened
    and its signals are already in the analytics store. Losing the
    hand-off file is a missing convenience; losing the scan because a
    disk was full is a lost observation.
    """
    rows = build_rows(signals, strategy_id=strategy_id, trading_day=trading_day,
                      session=session, run_id=run_id, generated_at=generated_at)
    if not rows:
        return rows
    try:
        path = candidates_path(trading_day, session)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row.as_dict(), sort_keys=True,
                                        default=str) + "\n")
    except Exception:  # noqa: BLE001 - a scan must survive a failed write
        logger.warning("could not publish %s candidates for %s/%s",
                       strategy_id, trading_day, session, exc_info=True)
    return rows


def read(trading_day: str, session: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every row published for a (day, session). Missing file -> []."""
    try:
        path = candidates_path(trading_day, session)
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    logger.warning("skipping unparseable candidate row in %s", path)
        return rows
    except Exception:  # noqa: BLE001
        logger.warning("could not read candidates for %s/%s", trading_day,
                       session, exc_info=True)
        return []
