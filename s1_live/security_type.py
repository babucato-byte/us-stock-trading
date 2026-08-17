"""What kind of instrument is this, according to KIS's own master file.

Why this module exists
---------------------
`universe.csv` carries symbol, name, exchange, tradable and shortable --
no security type. So nothing in this repository could distinguish a
common stock from an ETF, and an S1 scan over the full universe returns
both: a run over 600 names produced IUSV, KBE, MILN, BLCV, LEMB, IVOV,
HYGV, JPIE and JPLD alongside the equities. Buying any of those would
have taken on exactly the leveraged/inverse exposure the pilot forbids,
and `kis_live_trading.py`'s own docstring records that the only thing
standing between the pipeline and that outcome was an operator-curated
list.

The source of truth is KIS's published master, not a guess
----------------------------------------------------------
    https://new.real.download.dws.co.kr/common/master/{nas,nys,ams}mst.cod.zip

Field 9 of that file is the security type: 1=Index, 2=Stock, 3=ETP(ETF),
4=Warrant. That is a value the broker itself assigns, which is what makes
it usable as a gate.

Two approaches were deliberately rejected. A NAME heuristic ("does the
description contain 'ETF'") is a guess -- plenty of funds do not say so,
and "Osisko Development Corp." and "iShares Core S&P U.S. Value ETF" are
not separable by any rule that stays correct. Calling yfinance's `.info`
for `quoteType` per symbol is authoritative but costs ~12,900 network
round trips per refresh, so it is a research fallback and never the live
gate.

UNKNOWN is a refusal, never a default
-------------------------------------
A symbol absent from the master, or carrying a type this module does not
recognise, resolves to UNKNOWN and is refused. Treating an unrecognised
type as a common stock would make the one case we cannot see the one case
that trades.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Where the ingested master lives. Refreshed as a batch, joined locally.
CACHE_ENV = "S1_SECURITY_TYPE_CACHE"
DEFAULT_CACHE_NAME = "kis_security_types.json"

SOURCE_KIS_MASTER = "KIS_MASTER"

COMMON_STOCK = "COMMON_STOCK"
ETP = "ETP"
INDEX = "INDEX"
WARRANT = "WARRANT"
UNKNOWN = "UNKNOWN"

#: The ONLY type Stage 1 may buy. ETP is refused whole -- there is no
#: need to tell an ETF from a leveraged ETF when neither is permitted,
#: and asking that question would invite a wrong answer.
LIVE_ELIGIBLE_TYPES = frozenset({COMMON_STOCK})

SUPPORTED_EXCHANGES = frozenset({"NASDAQ", "NYSE", "AMEX", "NAS", "NYS", "AMS"})

#: How old the ingested master may be before it stops being evidence.
#: Listings change; a month-old file would silently vouch for a symbol
#: whose type had been reclassified.
MAX_CACHE_AGE_DAYS = 14

REASON_NOT_IN_MASTER = "INSTRUMENT_TYPE_UNKNOWN"
REASON_NOT_COMMON_STOCK = "SKIP_INSTRUMENT_NOT_ELIGIBLE"
REASON_UNSUPPORTED_EXCHANGE = "UNSUPPORTED_EXCHANGE"
REASON_CACHE_UNAVAILABLE = "SECURITY_TYPE_CACHE_UNAVAILABLE"
REASON_CACHE_STALE = "SECURITY_TYPE_CACHE_STALE"
REASON_CACHE_WRONG_SOURCE = "SECURITY_TYPE_SOURCE_NOT_KIS_MASTER"


class SecurityTypeUnavailable(Exception):
    """The classification could not be established. Callers must refuse
    to buy -- this is never "probably a stock"."""


@dataclass(frozen=True)
class Classification:
    symbol: str
    security_type: str
    security_type_raw: Optional[str] = None
    etp_type: Optional[str] = None
    exchange: Optional[str] = None
    source: str = SOURCE_KIS_MASTER
    asof: Optional[str] = None

    @property
    def live_eligible(self) -> bool:
        return (self.security_type in LIVE_ELIGIBLE_TYPES
                and (self.exchange or "").upper() in SUPPORTED_EXCHANGES)

    def ineligible_reason(self) -> Optional[str]:
        if self.security_type == UNKNOWN:
            return REASON_NOT_IN_MASTER
        if self.security_type not in LIVE_ELIGIBLE_TYPES:
            return REASON_NOT_COMMON_STOCK
        if (self.exchange or "").upper() not in SUPPORTED_EXCHANGES:
            return REASON_UNSUPPORTED_EXCHANGE
        return None

    def as_dict(self) -> Dict[str, Any]:
        return dict(vars(self), live_eligible=self.live_eligible,
                    ineligible_reason=self.ineligible_reason())


def cache_path() -> Path:
    configured = os.environ.get(CACHE_ENV)
    if configured:
        return Path(configured)
    root = os.environ.get("TRADING_PROJECT_ROOT") or "."
    return Path(root) / "logs" / "s1_live" / DEFAULT_CACHE_NAME


class SecurityTypeIndex:
    """A loaded master, with its own staleness and provenance checks."""

    def __init__(self, payload: Dict[str, Any]):
        self._symbols = payload.get("symbols") or {}
        self.source = payload.get("source")
        self.asof = payload.get("security_type_asof")
        self.counts = payload.get("counts") or {}

    def __len__(self):
        return len(self._symbols)

    def validate(self, *, now=None, max_age_days: int = MAX_CACHE_AGE_DAYS) -> None:
        """Raise unless this cache may be used as a live gate."""
        if self.source != SOURCE_KIS_MASTER:
            raise SecurityTypeUnavailable(
                f"{REASON_CACHE_WRONG_SOURCE}: source={self.source!r}")
        if not self._symbols:
            raise SecurityTypeUnavailable(f"{REASON_CACHE_UNAVAILABLE}: no symbols")
        stamp = _as_utc(self.asof)
        if stamp is None:
            raise SecurityTypeUnavailable(
                f"{REASON_CACHE_STALE}: unusable security_type_asof {self.asof!r}")
        current = now or datetime.now(timezone.utc)
        age = current - stamp
        if age > timedelta(days=max_age_days):
            raise SecurityTypeUnavailable(
                f"{REASON_CACHE_STALE}: master is {age.days} days old "
                f"(limit {max_age_days})")

    def classify(self, symbol) -> Classification:
        wanted = str(symbol or "").strip().upper()
        row = self._symbols.get(wanted)
        if row is None:
            return Classification(symbol=wanted, security_type=UNKNOWN, asof=self.asof)
        return Classification(
            symbol=wanted,
            security_type=row.get("security_type") or UNKNOWN,
            security_type_raw=row.get("security_type_raw"),
            etp_type=row.get("etp_type"),
            exchange=row.get("exchange_market") or row.get("exchange"),
            source=self.source or SOURCE_KIS_MASTER,
            asof=self.asof,
        )


def load_index(path=None) -> SecurityTypeIndex:
    """The ingested master, validated. Raises rather than returning empty."""
    target = Path(path) if path else cache_path()
    if not target.exists():
        raise SecurityTypeUnavailable(f"{REASON_CACHE_UNAVAILABLE}: {target}")
    try:
        payload = json.loads(target.read_text())
    except Exception as exc:
        raise SecurityTypeUnavailable(
            f"{REASON_CACHE_UNAVAILABLE}: {target} unreadable: {exc}") from exc
    index = SecurityTypeIndex(payload)
    index.validate()
    return index


def require_live_eligible(symbol, *, index=None) -> Classification:
    """The gate called immediately before an order. Raises on anything
    that is not a supported-exchange common stock."""
    idx = index or load_index()
    verdict = idx.classify(symbol)
    reason = verdict.ineligible_reason()
    if reason is not None:
        raise SecurityTypeUnavailable(
            f"{reason}: {verdict.symbol} is {verdict.security_type}"
            + (f"/{verdict.etp_type}" if verdict.etp_type else ""))
    return verdict


def _as_utc(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
