"""Which symbols are worth looking at INTRADAY (spec sections 13, 14).

The problem this exists for
---------------------------
The daily profile can afford to walk the whole universe after the close.
The intraday profiles cannot. The ORB scanner's opening range is only
meaningful for a window of minutes after 09:45, and a 13,362-symbol pass
at the measured rate would take hours -- the answer would arrive long
after the setup it describes had resolved. Section 13 forbids that
structure outright.

So the intraday profiles run against an ACTIVE universe of a few hundred
names, and this module is what produces it.

Three layers, deliberately not mixed (section 14)
-------------------------------------------------
    DATA ELIGIBILITY   can the features be computed at all?
                       -> scanners/base/eligibility.py
    ACTIVITY           is this symbol liquid enough to be worth an
                       intraday fetch?           -> this module
    STRATEGY FILTER    does it meet a scanner's conditions?
                       -> each scanner's config.json

This module holds the middle layer only. It ranks on dollar volume,
which is a measure of whether a name TRADES, not of whether it is going
anywhere: no price direction, no volume ratio, no gap, no momentum.
Those all live in scanner configs, and putting one here would create a
strategy filter invisible to every scanner's recorded parameters -- a
hidden condition that month-1 analysis could never see, and that no
config fingerprint would capture.

Where the numbers come from
---------------------------
Nowhere new. The daily profile already computes price and 20-day average
volume for every symbol it scans; this records their product as it goes.
The intraday profiles then read yesterday's ranking instead of
discovering it, so selecting the active universe costs no fetches at
all.

That ordering is a real dependency: the intraday pool is only as fresh
as the last daily run. `max_age_days` refuses to build a pool from
stale rankings rather than silently scanning last month's most active
names.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from scanners.base.result_store import analytics_dir

logger = logging.getLogger(__name__)

ACTIVITY_SUBDIR = "activity"

#: Default size of the intraday pool. Section 29 asks for "tens to
#: hundreds" of symbols and a run under five minutes; at the measured
#: intraday cost this lands comfortably inside that.
DEFAULT_POOL_SIZE = 300

#: Refuse to build a pool from a ranking older than this. A stale
#: ranking is not obviously wrong -- it produces a plausible list of
#: yesteryear's active names -- so it has to be rejected on age rather
#: than noticed by a reader.
DEFAULT_MAX_AGE_DAYS = 5


@dataclass
class ActivityRecord:
    symbol: str
    trading_day: str
    price: Optional[float] = None
    avg_volume: Optional[float] = None
    dollar_volume: Optional[float] = None
    updated_at: Optional[str] = None


def store_path(provider: str) -> Path:
    directory = analytics_dir() / ACTIVITY_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(provider))
    return directory / f"{safe or 'unknown'}.json"


class ActivityStore:
    """Per-provider activity ranking, written by daily, read by intraday."""

    def __init__(self, provider: str, records: Optional[Dict[str, ActivityRecord]] = None):
        self.provider = provider
        self._records: Dict[str, ActivityRecord] = dict(records or {})
        self._dirty = False

    @classmethod
    def load(cls, provider: str) -> "ActivityStore":
        path = store_path(provider)
        if not path.exists():
            return cls(provider)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("activity cache unreadable at %s (%s); starting empty", path, exc)
            return cls(provider)
        records = {}
        for symbol, row in (payload.get("symbols") or {}).items():
            try:
                records[symbol] = ActivityRecord(**row)
            except TypeError:
                continue
        return cls(provider, records)

    def save(self) -> Optional[Path]:
        if not self._dirty:
            return None
        path = store_path(self.provider)
        payload = {
            "provider": self.provider,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "symbols": {s: asdict(r) for s, r in self._records.items()},
        }
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("could not save activity cache to %s: %s", path, exc)
            return None
        self._dirty = False
        return path

    def note(self, symbol: str, *, trading_day: str, price=None, avg_volume=None) -> None:
        """Record one symbol's liquidity from a daily feature pass."""
        dollar_volume = None
        if price is not None and avg_volume is not None:
            try:
                dollar_volume = float(price) * float(avg_volume)
            except (TypeError, ValueError):
                dollar_volume = None
        self._records[str(symbol).strip().upper()] = ActivityRecord(
            symbol=str(symbol).strip().upper(),
            trading_day=str(trading_day),
            price=None if price is None else float(price),
            avg_volume=None if avg_volume is None else float(avg_volume),
            dollar_volume=dollar_volume,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._dirty = True

    def active_symbols(
        self,
        *,
        limit: int = DEFAULT_POOL_SIZE,
        min_dollar_volume: float = 0.0,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
        today: Optional[date] = None,
    ) -> List[str]:
        """The most liquid symbols with a recent enough ranking.

        Returns `[]` when nothing qualifies, and the caller must treat
        that as "no pool available" rather than "no active symbols" --
        an empty intraday universe is an operational fact (the daily run
        has not populated the ranking yet), not a market observation.
        """
        moment = today or datetime.now(timezone.utc).date()
        cutoff = moment - timedelta(days=int(max_age_days))
        usable = []
        for record in self._records.values():
            if record.dollar_volume is None or record.dollar_volume < min_dollar_volume:
                continue
            try:
                if date.fromisoformat(str(record.trading_day)) < cutoff:
                    continue
            except (TypeError, ValueError):
                continue
            usable.append(record)
        usable.sort(key=lambda r: r.dollar_volume, reverse=True)
        return [r.symbol for r in usable[: max(0, int(limit))]]

    def summary(self, *, today: Optional[date] = None) -> Dict[str, Any]:
        moment = today or datetime.now(timezone.utc).date()
        days = sorted({str(r.trading_day) for r in self._records.values()})
        return {
            "provider": self.provider,
            "ranked_symbols": len(self._records),
            "latest_trading_day": days[-1] if days else None,
            "as_of": moment.isoformat(),
        }


class NullActivityStore(ActivityStore):
    """No-op, so the runner keeps a single code path when disabled."""

    def __init__(self, provider: str = "none"):
        super().__init__(provider)

    def note(self, symbol, **kwargs) -> None:
        return None

    def save(self):
        return None
