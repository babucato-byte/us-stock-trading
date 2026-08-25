"""Which symbols are worth spending a provider call on, decided offline.

The measurement that forced this
--------------------------------
A full pass over the raw 12,886-name universe priced 57% at a fixed
fetch interval and 47% with an adaptive one, while the same code priced
90% over 4,000 names. The provider throttles on sustained volume, so the
binding variable is how many symbols are requested, not how politely.
Fetching fewer is the fix that works; pacing was the fix that did not.

Roughly 7,200 of those names can never be an S6 entry -- ETFs, closed-
end funds, preferred lines, warrants, rights, units and notes -- and
that is knowable from `universe.csv`, which already carries `name`,
`exchange` and `tradable`. Establishing it costs no network at all.

This is a COST filter, not the eligibility decision
---------------------------------------------------
The authoritative answer is KIS's own security master, and it lives on
the trading node, which re-checks every candidate before an order is
considered. That check has already rejected AGG and VCSH in production.

So the two kinds of mistake here are not symmetric. Wrongly INCLUDING an
ETF costs one provider call and is caught downstream. Wrongly EXCLUDING
a real common stock removes it from discovery entirely and nothing
downstream can recover it -- that name simply never gets looked at. The
patterns below are therefore deliberately conservative: they match
issue-type wording rather than guessing from ticker suffixes, because
`BMNR`, `SDOT` and `XPON` all look exotic and all three are ordinary
common stock.

Exchanges
---------
NYSE, NASDAQ and AMEX only. ARCA and BATS are ETF venues, and the
trading node already refuses ARCA outright -- "AGG is listed on 'ARCA',
which this system does not trade" -- so fetching them buys nothing an
order could ever use.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "s6_eligible_universe_v1"

#: Venues an S6 order can actually reach.
TRADEABLE_EXCHANGES = frozenset({"NYSE", "NASDAQ", "AMEX"})

#: Exclusion reasons, most specific first. Order matters only for
#: reporting -- a symbol is excluded once, under the first reason that
#: recognises it, so the counts sum to the number excluded.
EXCLUSION_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("ETP", r"\bETF\b|\bETN\b|ISHARES|VANGUARD|SPDR|INVESCO|PROSHARES|"
            r"DIREXION|GLOBAL X|WISDOMTREE|FIRST TRUST|INDEX FUND"),
    ("PREFERRED", r"PREFERRED|\bPFD\b|DEPOSITARY SH"),
    ("WARRANT", r"\bWARRANT"),
    ("RIGHT", r"\bRIGHTS?\b"),
    ("UNIT", r"\bUNITS?\b"),
    ("NOTE", r"\bNOTES?\b|\bDEBENTURE"),
    ("CLOSED_END_FUND", r"CLOSED[- ]END"),
)

_COMPILED = tuple((reason, re.compile(pattern))
                  for reason, pattern in EXCLUSION_PATTERNS)

REASON_NOT_TRADABLE = "NOT_TRADABLE"
REASON_NON_US_EXCHANGE = "NON_US_EXCHANGE"
REASON_NO_METADATA = "NO_METADATA"
ELIGIBLE = "ELIGIBLE"

#: How long the cache stands. Security type and listing venue are not
#: intraday facts -- a symbol does not become an ETF at lunchtime -- so
#: rebuilding this every hour would re-derive an unchanged answer over
#: 12,886 rows for nothing. A trading day is the natural period, and the
#: universe file's own mtime forces a rebuild when it changes.
DEFAULT_MAX_AGE_HOURS = 20


def classify(name, exchange, tradable=True) -> str:
    """`ELIGIBLE`, or the reason this symbol is not worth a call."""
    if not tradable:
        return REASON_NOT_TRADABLE
    if not name:
        # No description to judge. Kept if the venue is right: a missing
        # name is a gap in the metadata, not evidence about the issue,
        # and excluding on it would silently drop real listings.
        return (ELIGIBLE if str(exchange or "").upper() in TRADEABLE_EXCHANGES
                else REASON_NON_US_EXCHANGE)
    if str(exchange or "").upper() not in TRADEABLE_EXCHANGES:
        return REASON_NON_US_EXCHANGE
    upper = str(name).upper()
    for reason, pattern in _COMPILED:
        if pattern.search(upper):
            return reason
    return ELIGIBLE


def build(rows) -> Dict[str, Any]:
    """`rows` are dicts with symbol/name/exchange/tradable."""
    eligible: List[str] = []
    counts: Dict[str, int] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        verdict = classify(row.get("name"), row.get("exchange"),
                           bool(row.get("tradable", True)))
        if verdict == ELIGIBLE:
            eligible.append(symbol)
        else:
            counts[verdict] = counts.get(verdict, 0) + 1

    total = len(eligible) + sum(counts.values())
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_universe_count": total,
        "eligible_count": len(eligible),
        "excluded_count": sum(counts.values()),
        "exclude_reason_counts": dict(sorted(counts.items(),
                                             key=lambda kv: -kv[1])),
        "symbols": eligible,
    }


def default_path() -> Path:
    return Path("logs/discovery/eligible_universe.json")


def write(document, path=None) -> Path:
    from discovery import manifest as manifest_module

    return manifest_module.write(document, path or default_path())


def read(path=None) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(Path(path or default_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_stale(document, *, now=None, max_age_hours=DEFAULT_MAX_AGE_HOURS,
             universe_mtime=None) -> bool:
    """Age, schema, or a universe file newer than the cache."""
    if not document or document.get("schema_version") != SCHEMA_VERSION:
        return True
    try:
        made = datetime.fromisoformat(
            str(document["generated_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return True
    if made.tzinfo is None:
        made = made.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    if moment - made > timedelta(hours=float(max_age_hours)):
        return True
    if universe_mtime is not None and universe_mtime > made:
        # The universe file was rewritten after this cache was built, so
        # the cache describes a different set of symbols than the one
        # the scan is about to walk.
        return True
    return False


def load_or_build(*, path=None, rows=None, now=None,
                  max_age_hours=DEFAULT_MAX_AGE_HOURS) -> Dict[str, Any]:
    """The cached eligible universe, rebuilt only when it has to be."""
    target = Path(path or default_path())
    cached = read(target)

    universe_mtime = None
    try:
        from scanners.universe import universe_path

        source = Path(universe_path())
        if source.exists():
            universe_mtime = datetime.fromtimestamp(source.stat().st_mtime,
                                                    tz=timezone.utc)
    except Exception:  # noqa: BLE001 - an unreadable mtime is not a reason
        universe_mtime = None                        # to refuse the cache

    if not is_stale(cached, now=now, max_age_hours=max_age_hours,
                    universe_mtime=universe_mtime):
        return cached

    if rows is None:
        import pandas as pd

        from scanners.universe import universe_path

        frame = pd.read_csv(universe_path())
        rows = frame.to_dict("records")

    document = build(rows)
    try:
        write(document, target)
    except Exception:  # noqa: BLE001 - a cache that cannot be written is
        logger.warning("could not cache the eligible universe at %s",
                       target, exc_info=True)      # still a usable answer
    logger.info("eligible universe: %s of %s symbols (excluded %s)",
                document["eligible_count"], document["source_universe_count"],
                document["exclude_reason_counts"])
    return document
