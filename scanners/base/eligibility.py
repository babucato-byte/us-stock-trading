"""Which symbols CAN be scanned, remembered between runs.

The problem
-----------
In the 200-symbol server benchmark, 59 of 200 symbols (29.5%) produced
nothing a scanner could judge: 7 the provider would not serve at all, 49
without enough daily history for an HMA200, 3 with unusable columns. Every
one of those cost a network round trip and a partial feature pass, and
every one of them would cost the same again on the next run, and the run
after that. Over 13,362 symbols that is roughly 3,900 symbols of pure
waste per scan, forever.

The fix is memory, not cleverness: record WHY a symbol could not be
judged and when it is worth asking again.

This is data eligibility, not strategy
--------------------------------------
Section 6 draws the line and this module stays on one side of it. The
only question asked here is:

    is there enough usable data to COMPUTE this scanner's features?

Never "is the trend up", "is volume elevated", "is ADX above 20". A
symbol excluded here was never evaluated by a scanner at all, so nothing
recorded here can change which symbols pass a strategy condition -- it
only changes which symbols were cheap enough to ask about. Putting a
strategy threshold in this file would silently become a hidden filter on
month-1 data, invisible in every scanner's config.

Nothing is excluded permanently
-------------------------------
Every record carries `next_check`. A symbol that lacks history today
acquires it one session at a time, and a newly listed name must not be
locked out of the universe because the first scan found it too young.
For `insufficient_history` the recheck date is CALCULATED from how many
bars are missing rather than guessed, so a symbol 3 bars short returns in
days while one 200 bars short is not re-fetched 200 times to be told the
same thing.

A provider outage is treated as transient and rechecked quickly, because
the alternative -- a bad afternoon at the vendor evicting a third of the
universe for a month -- is far worse than a few wasted fetches.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from scanners.base.result_store import analytics_dir

logger = logging.getLogger(__name__)

ELIGIBILITY_SUBDIR = "eligibility"

# --- reasons -------------------------------------------------------------
ELIGIBLE = "eligible"
INSUFFICIENT_HISTORY = "insufficient_history"
NON_NUMERIC_OHLCV = "non_numeric_ohlcv"
EMPTY_HISTORY = "empty_history"
PROVIDER_UNAVAILABLE = "provider_unavailable"
UNSUPPORTED_SYMBOL = "unsupported_symbol"
STALE_HISTORY = "stale_history"

#: How long a verdict stands before it is re-tested, per reason.
#:
#: The asymmetry is deliberate. A transient provider failure is cheap to
#: retry and expensive to believe, so it expires almost immediately. A
#: structural problem (columns that are not prices) is unlikely to fix
#: itself within a day but might within a week. `eligible` is re-tested
#: periodically too -- a symbol can be delisted, and a stale "yes" costs
#: a fetch per run until someone notices.
RECHECK_DAYS = {
    ELIGIBLE: 7,
    PROVIDER_UNAVAILABLE: 1,
    EMPTY_HISTORY: 3,
    NON_NUMERIC_OHLCV: 7,
    UNSUPPORTED_SYMBOL: 30,
    STALE_HISTORY: 3,
    # INSUFFICIENT_HISTORY is computed from the shortfall; see below.
}

#: Fallback when the shortfall cannot be computed.
DEFAULT_RECHECK_DAYS = 7

#: Calendar days per trading day, for turning "needs N more bars" into a
#: date. Five sessions per seven calendar days, rounded up so the recheck
#: lands after the bars exist rather than a day before.
CALENDAR_DAYS_PER_SESSION = 7.0 / 5.0

#: Never wait longer than this, whatever the shortfall. A symbol needing
#: 200 more sessions would otherwise not be looked at for nine months,
#: and universes get rebuilt more often than that.
MAX_RECHECK_DAYS = 90


@dataclass
class EligibilityRecord:
    symbol: str
    eligible: bool
    reason: str
    provider: str
    last_checked: str
    next_check: str
    history_bars: Optional[int] = None
    required_bars: Optional[int] = None
    detail: Optional[str] = None

    def due(self, today: Optional[date] = None) -> bool:
        """Is this verdict old enough to re-test?"""
        moment = today or datetime.now(timezone.utc).date()
        try:
            return date.fromisoformat(self.next_check) <= moment
        except (TypeError, ValueError):
            # An unreadable date must not pin a symbol out of the
            # universe forever -- re-test it.
            return True


def recheck_days_for(reason: str, *, history_bars=None, required_bars=None) -> int:
    """How long this verdict stands.

    For `insufficient_history` the answer is arithmetic rather than a
    guess: a symbol short by N sessions cannot become eligible before N
    sessions have passed, so asking sooner is guaranteed to get the same
    answer at the cost of a round trip.
    """
    if reason == INSUFFICIENT_HISTORY and history_bars is not None and required_bars:
        missing = int(required_bars) - int(history_bars)
        if missing <= 0:
            return 1
        days = int(missing * CALENDAR_DAYS_PER_SESSION) + 1
        return max(1, min(days, MAX_RECHECK_DAYS))
    return RECHECK_DAYS.get(reason, DEFAULT_RECHECK_DAYS)


def make_record(symbol: str, *, eligible: bool, reason: str, provider: str,
                history_bars=None, required_bars=None, detail=None,
                today: Optional[date] = None) -> EligibilityRecord:
    moment = today or datetime.now(timezone.utc).date()
    days = recheck_days_for(reason, history_bars=history_bars,
                            required_bars=required_bars)
    return EligibilityRecord(
        symbol=str(symbol).strip().upper(),
        eligible=bool(eligible),
        reason=reason,
        provider=provider,
        last_checked=moment.isoformat(),
        next_check=(moment + timedelta(days=days)).isoformat(),
        history_bars=None if history_bars is None else int(history_bars),
        required_bars=None if required_bars is None else int(required_bars),
        detail=detail,
    )


def classify_data_error(message: str) -> str:
    """Map a `ScannerDataError` message onto an eligibility reason.

    Matching on the message is not elegant, but the alternative --
    threading a reason code through every raise site in the feature pass
    -- would spread eligibility concerns across code whose job is
    indicators. The mapping is conservative: anything unrecognised
    becomes `EMPTY_HISTORY`, which has a SHORT recheck, so a
    misclassification costs a wasted fetch rather than a long exclusion.
    """
    text = (message or "").lower()
    if "non_numeric_ohlcv" in text or "not numeric" in text:
        return NON_NUMERIC_OHLCV
    if "daily bars, need" in text or "not computable from" in text:
        return INSUFFICIENT_HISTORY
    if "old, limit" in text:
        return STALE_HISTORY
    if "no daily bars" in text or "no usable" in text:
        return EMPTY_HISTORY
    return EMPTY_HISTORY


def store_path(provider: str) -> Path:
    directory = analytics_dir() / ELIGIBILITY_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(provider))
    return directory / f"{safe or 'unknown'}.json"


class EligibilityStore:
    """Per-provider eligibility, loaded once and saved once per run.

    Keyed by provider because eligibility is a statement about what a
    VENDOR will serve. A symbol Yahoo has no history for may be perfectly
    available from another provider, and merging the two would let one
    vendor's gaps silently shrink another's universe -- the same
    cross-provider contamination section 12 keeps out of the analytics.
    """

    def __init__(self, provider: str, records: Optional[Dict[str, EligibilityRecord]] = None):
        self.provider = provider
        self._records: Dict[str, EligibilityRecord] = dict(records or {})
        self._dirty = False

    # ---- persistence ----
    @classmethod
    def load(cls, provider: str) -> "EligibilityStore":
        path = store_path(provider)
        if not path.exists():
            return cls(provider)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A corrupt cache must degrade to "no cache", never to a
            # crashed scan: the cache is an optimisation, not data.
            logger.warning("eligibility cache unreadable at %s (%s); starting empty",
                           path, exc)
            return cls(provider)
        records = {}
        for symbol, row in (payload.get("symbols") or {}).items():
            try:
                records[symbol] = EligibilityRecord(**row)
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
            logger.warning("could not save eligibility cache to %s: %s", path, exc)
            return None
        self._dirty = False
        return path

    # ---- queries ----
    def get(self, symbol: str) -> Optional[EligibilityRecord]:
        return self._records.get(str(symbol).strip().upper())

    def should_skip(self, symbol: str, *, today: Optional[date] = None) -> bool:
        """Skip this symbol entirely -- no fetch, no feature pass?

        Only a record that is BOTH ineligible AND not yet due is skipped.
        An eligible record never causes a skip: it is a statement that
        the data exists, not a licence to stop looking at the symbol.
        """
        record = self.get(symbol)
        if record is None or record.eligible:
            return False
        return not record.due(today)

    def record(self, record: EligibilityRecord) -> None:
        self._records[record.symbol] = record
        self._dirty = True

    def note_eligible(self, symbol: str, *, history_bars=None, required_bars=None,
                      today: Optional[date] = None) -> None:
        self.record(make_record(symbol, eligible=True, reason=ELIGIBLE,
                                provider=self.provider, history_bars=history_bars,
                                required_bars=required_bars, today=today))

    def note_ineligible(self, symbol: str, reason: str, *, history_bars=None,
                        required_bars=None, detail=None,
                        today: Optional[date] = None) -> None:
        self.record(make_record(symbol, eligible=False, reason=reason,
                                provider=self.provider, history_bars=history_bars,
                                required_bars=required_bars, detail=detail, today=today))

    # ---- reporting ----
    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for record in self._records.values():
            counts[record.reason] = counts.get(record.reason, 0) + 1
        eligible = sum(1 for r in self._records.values() if r.eligible)
        return {
            "provider": self.provider,
            "known_symbols": len(self._records),
            "eligible": eligible,
            "ineligible": len(self._records) - eligible,
            "by_reason": dict(sorted(counts.items())),
        }

    def eligible_symbols(self, candidates: Iterable[str], *,
                         today: Optional[date] = None) -> List[str]:
        """`candidates` minus the ones a current record rules out."""
        return [s for s in candidates if not self.should_skip(s, today=today)]


class NullEligibilityStore(EligibilityStore):
    """A store that remembers nothing.

    Used when eligibility is switched off, so the runner keeps one code
    path instead of sprinkling `if store is not None` through the scan
    loop -- the branch that would eventually be got wrong.
    """

    def __init__(self, provider: str = "none"):
        super().__init__(provider)

    def should_skip(self, symbol, *, today=None) -> bool:
        return False

    def record(self, record) -> None:
        return None

    def save(self):
        return None
