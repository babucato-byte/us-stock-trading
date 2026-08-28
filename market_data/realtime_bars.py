"""Trades in, one-minute bars out, one session at a time.

The bars S6's features are computed from. Everything here follows from
three decisions, and each of them is a decision about what to do when
the data is not what we hoped.

Volume is summed from EVOL, not differenced from TVOL
-----------------------------------------------------
Both are available and they should agree. Summing our own trades is
correct whatever KIS's cumulative counter means, survives a reconnect
that costs us the first trade of a window, and cannot be thrown off by
a counter that resets on a session boundary. TVOL is still tracked and
the two are compared, because a persistent disagreement is a real
signal about the feed -- it just is not allowed to silently become the
volume a strategy trades on.

VWAP is computed from the trades in THIS session
------------------------------------------------
TAMT/TVOL is one division and would be free, but only if KIS's
cumulative fields are scoped to the session we are in, and that is a
question about someone else's semantics. Observed on 2026-08-28: AAPL's
TVOL read 6,447 during the daytime session against a regular-session
volume in the tens of millions, so the counters are clearly not
day-cumulative -- but "clearly not" is inference, and this is a number a
strategy would size a position from. So VWAP comes from
`sum(price*size)/sum(size)` over trades this session, and TAMT/TVOL is
recorded beside it as a cross-check.

A gap is a gap, never a zero
----------------------------
A disconnected minute has no trades, and a minute with no trades looks
exactly like a minute in which nothing traded. The first is missing
data and the second is information. Bars that were never observed are
absent and the gap is recorded; nothing here manufactures an empty bar
to make a series look continuous.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from market_data import kis_hdfscnt0 as wire

logger = logging.getLogger(__name__)

SOURCE = wire.SOURCE

#: Feed states. STALE and DISCONNECTED both mean "do not trade on this",
#: and they are kept apart because they need different operator action.
FEED_LIVE = "LIVE"
FEED_STALE = "STALE"
FEED_DISCONNECTED = "DISCONNECTED"
FEED_UNKNOWN = "UNKNOWN"

#: How old the newest trade may be before the feed is not LIVE. A quiet
#: premarket genuinely goes minutes without a print, so this is
#: deliberately not aggressive; the ENTRY path applies its own, tighter
#: freshness rule on top.
DEFAULT_STALE_AFTER_SECONDS = 180.0


class FeedLag:
    """How far behind the feed actually runs, measured continuously.

    The constant is 70 seconds and it is not trusted. "Delayed" could
    mean fifteen minutes on another account, on another day, or after a
    KIS change, and a freshness rule built on a number nobody re-checks
    is the same mistake as the zero-volume assumption -- a measurement
    frozen into a belief.

    So the lag is observed per trade and kept as a distribution. The
    median says what normal is; p95 and max say what the freshness
    threshold has to tolerate before it starts rejecting good data.
    """

    #: Enough to characterise a session without unbounded growth.
    WINDOW = 512

    def __init__(self):
        self._samples: List[float] = []

    def observe(self, *, market_timestamp, received_at):
        if market_timestamp is None or received_at is None:
            return None
        lag = (received_at - market_timestamp).total_seconds()
        # A negative lag means the clocks disagree, not that data arrived
        # before it happened. Recording it would corrupt the median that
        # a freshness rule is built on.
        if lag < 0:
            return None
        self._samples.append(lag)
        if len(self._samples) > self.WINDOW:
            del self._samples[:len(self._samples) - self.WINDOW]
        return lag

    def samples(self):
        return list(self._samples)

    def restore(self, values):
        self._samples = [float(v) for v in values
                         if isinstance(v, (int, float))][-self.WINDOW:]

    def describe(self) -> dict:
        if not self._samples:
            return {"samples": 0, "median": None, "p95": None, "max": None}
        ordered = sorted(self._samples)
        return {
            "samples": len(ordered),
            "median": ordered[len(ordered) // 2],
            "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
            "max": ordered[-1],
        }


@dataclass
class Bar:
    symbol: str
    session: str
    minute: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int
    first_trade_at: datetime
    last_trade_at: datetime
    source: str = SOURCE
    #: KIS's own cumulative counters at the first and last trade of this
    #: bar. Kept for the cross-check, never used as the volume.
    cumulative_first: Optional[float] = None
    cumulative_last: Optional[float] = None
    amount_first: Optional[float] = None
    amount_last: Optional[float] = None

    @property
    def market_data_asof(self) -> datetime:
        return self.last_trade_at

    @property
    def cumulative_delta(self) -> Optional[float]:
        if self.cumulative_first is None or self.cumulative_last is None:
            return None
        return self.cumulative_last - self.cumulative_first

    def as_record(self) -> dict:
        return {
            "symbol": self.symbol, "session": self.session,
            "minute": self.minute.isoformat(),
            "open": self.open, "high": self.high, "low": self.low,
            "close": self.close, "volume": self.volume,
            "trade_count": self.trade_count,
            "first_trade_at": self.first_trade_at.isoformat(),
            "last_trade_at": self.last_trade_at.isoformat(),
            "market_data_asof": self.market_data_asof.isoformat(),
            "source": self.source,
            "cumulative_delta": self.cumulative_delta,
        }


def parse_trade_time(record, *, now=None):
    """The trade's own local timestamp, as UTC.

    KIS sends XYMD/XHMS in the LOCAL market's time. Those are what a bar
    must be keyed on -- using arrival time would put a trade in whatever
    minute our process happened to read it, which is a different bar
    whenever the socket is behind.
    """
    date_text = str(record.get(wire.FIELD_LOCAL_DATE) or "").strip()
    time_text = str(record.get(wire.FIELD_LOCAL_TIME) or "").strip()
    if len(date_text) != 8 or len(time_text) != 6:
        return None
    try:
        naive = datetime.strptime(date_text + time_text, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    try:
        import zoneinfo

        eastern = zoneinfo.ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001 - without tz data the stamp cannot
        # be placed on a timeline, and guessing UTC would silently shift
        # every bar by the offset.
        return None
    return naive.replace(tzinfo=eastern).astimezone(timezone.utc)


@dataclass
class SessionAccumulator:
    """One symbol, one session. Reset when the session changes.

    Session isolation is the whole point: a premarket VWAP that includes
    yesterday's regular-session prints is not a premarket VWAP, and a
    volume expansion measured across a session boundary compares two
    different markets.
    """

    symbol: str
    session: str
    bars: Dict[datetime, Bar] = field(default_factory=dict)
    price_volume: float = 0.0
    volume: float = 0.0
    trade_count: int = 0
    first_cumulative: Optional[float] = None
    #: The first trade's own size. KIS's cumulative counter already
    #: INCLUDES it, so the delta from that first reading spans every
    #: trade except the one it was taken at.
    first_trade_size: Optional[float] = None
    last_cumulative: Optional[float] = None
    first_amount: Optional[float] = None
    last_amount: Optional[float] = None
    last_trade_at: Optional[datetime] = None

    def add(self, *, price, size, at, cumulative=None, amount=None):
        minute = at.replace(second=0, microsecond=0)
        bar = self.bars.get(minute)
        if bar is None:
            self.bars[minute] = Bar(
                symbol=self.symbol, session=self.session, minute=minute,
                open=price, high=price, low=price, close=price,
                volume=size, trade_count=1,
                first_trade_at=at, last_trade_at=at,
                cumulative_first=cumulative, cumulative_last=cumulative,
                amount_first=amount, amount_last=amount)
        else:
            bar.high = max(bar.high, price)
            bar.low = min(bar.low, price)
            bar.close = price
            bar.volume += size
            bar.trade_count += 1
            bar.last_trade_at = max(bar.last_trade_at, at)
            if cumulative is not None:
                bar.cumulative_last = cumulative
                if bar.cumulative_first is None:
                    bar.cumulative_first = cumulative
            if amount is not None:
                bar.amount_last = amount
                if bar.amount_first is None:
                    bar.amount_first = amount

        self.price_volume += price * size
        self.volume += size
        self.trade_count += 1
        if cumulative is not None:
            self.last_cumulative = cumulative
            if self.first_cumulative is None:
                self.first_cumulative = cumulative
                self.first_trade_size = size
        if amount is not None:
            self.last_amount = amount
            if self.first_amount is None:
                self.first_amount = amount
        if self.last_trade_at is None or at > self.last_trade_at:
            self.last_trade_at = at

    @property
    def vwap(self) -> Optional[float]:
        """Session VWAP from this session's own trades.

        None when there is no volume -- never a division that invents a
        denominator, because a VWAP is a price a strategy compares
        against and a fabricated one is worse than none.
        """
        if self.volume <= 0:
            return None
        return self.price_volume / self.volume

    @property
    def vwap_from_kis_cumulative(self) -> Optional[float]:
        """The same number KIS's own counters imply, for comparison.

        Only meaningful if TAMT and TVOL are scoped to this session.
        That is exactly what is unverified, which is why this is a
        cross-check and not the answer.
        """
        if not self.last_amount or not self.last_cumulative:
            return None
        return self.last_amount / self.last_cumulative

    def volume_cross_check(self) -> dict:
        """Our summed volume against KIS's cumulative delta.

        A disagreement does not invalidate the bars -- the sum is the
        authority -- but it is recorded, because a persistent one means
        either dropped frames or a counter that does not mean what we
        think it means, and both are worth knowing before the next
        surprise.
        """
        delta = None
        if self.first_cumulative is not None and self.last_cumulative is not None:
            delta = self.last_cumulative - self.first_cumulative

        # KIS's counter already includes the first trade, so the reading
        # taken AT it corresponds to a window that starts one trade in.
        # Backing that size out gives a span comparable with our sum;
        # comparing the raw delta instead would report a one-trade
        # disagreement on every perfectly healthy stream.
        comparable = None
        if delta is not None and self.first_trade_size is not None:
            comparable = delta + self.first_trade_size

        agrees = None
        if comparable is not None and self.volume > 0:
            agrees = abs(comparable - self.volume) <= max(
                1.0, self.volume * 0.02)
        return {"summed_volume": self.volume, "kis_cumulative_delta": delta,
                "kis_span_including_first": comparable,
                "agrees": agrees, "trade_count": self.trade_count}

    def ordered_bars(self) -> List[Bar]:
        return [self.bars[key] for key in sorted(self.bars)]


class RealtimeBarStore:
    """Every symbol's current-session bars, and the feed's health.

    Deliberately in memory with an explicit snapshot/restore: the
    collector persists between runs so a restart does not read as "this
    session had no volume", and nothing here writes to the database on
    the path that also paces broker calls.
    """

    def __init__(self, *, stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS):
        self._accumulators: Dict[tuple, SessionAccumulator] = {}
        self._stale_after = float(stale_after_seconds)
        self.connected_at: Optional[datetime] = None
        self.disconnected_at: Optional[datetime] = None
        self.gaps: List[dict] = []
        self.layout_mismatches = 0
        self.dropped_unparsable = 0
        self.feed_lag = FeedLag()
        #: Which feed each symbol's data actually arrived on, so a dual
        #: subscription can never have its data credited to the wrong one.
        self.feeds_seen: Dict[str, str] = {}

    # -- ingest ----------------------------------------------------------

    def add_trade(self, record, *, session, now=None):
        """One parsed HDFSCNT0 record. Returns the bar minute, or None."""
        if record.get("layout_mismatch"):
            self.layout_mismatches += 1
            return None
        symbol = str(record.get("SYMB") or record.get("RSYM") or "").upper()
        price = wire.as_number(record.get(wire.FIELD_PRICE))
        size = wire.as_number(record.get(wire.FIELD_TRADE_SIZE))
        at = parse_trade_time(record, now=now)
        if not symbol or price is None or size is None or at is None:
            self.dropped_unparsable += 1
            return None
        if size <= 0:
            # A zero-size print is not a trade for volume purposes, and
            # counting it would inflate trade_count without volume.
            return None

        key = (symbol, session)
        accumulator = self._accumulators.get(key)
        if accumulator is None:
            # A new session gets a NEW accumulator rather than a reset
            # one, so a late trade from the previous session cannot land
            # in it.
            accumulator = SessionAccumulator(symbol=symbol, session=session)
            self._accumulators[key] = accumulator
        accumulator.add(price=price, size=size, at=at,
                        cumulative=wire.as_number(record.get(wire.FIELD_CUMULATIVE)),
                        amount=wire.as_number(record.get(wire.FIELD_AMOUNT)))

        # Attribution and lag, per trade. `at` is the market's own
        # timestamp and `received_at` is ours, so the difference is the
        # feed's real delay rather than an assumption about its name.
        self.feed_lag.observe(market_timestamp=at,
                              received_at=now or datetime.now(timezone.utc))
        feed = (wire.feed_of(record.get("RSYM"))
                or wire.feed_of(record.get("tr_key"))
                or wire.feed_of(record.get("TSYM")))
        if feed:
            self.feeds_seen[symbol] = feed
        return at.replace(second=0, microsecond=0)

    # -- read ------------------------------------------------------------

    def accumulator(self, symbol, session) -> Optional[SessionAccumulator]:
        return self._accumulators.get((str(symbol).upper(), session))

    def bars(self, symbol, session) -> List[Bar]:
        found = self.accumulator(symbol, session)
        return found.ordered_bars() if found else []

    def feed_status(self, *, now=None) -> str:
        current = now or datetime.now(timezone.utc)
        if self.disconnected_at is not None:
            return FEED_DISCONNECTED
        newest = self.newest_trade_at()
        if newest is None:
            return FEED_UNKNOWN
        age = (current - newest).total_seconds()
        return FEED_LIVE if age <= self._stale_after else FEED_STALE

    def newest_trade_at(self) -> Optional[datetime]:
        stamps = [a.last_trade_at for a in self._accumulators.values()
                  if a.last_trade_at is not None]
        return max(stamps) if stamps else None

    def describe(self, *, now=None) -> dict:
        return {
            "source": SOURCE,
            "feed_status": self.feed_status(now=now),
            "symbols": len(self._accumulators),
            "newest_trade_at": (self.newest_trade_at().isoformat()
                                if self.newest_trade_at() else None),
            "gaps": list(self.gaps),
            "layout_mismatches": self.layout_mismatches,
            "dropped_unparsable": self.dropped_unparsable,
            "feed_lag_seconds": self.feed_lag.describe(),
            "feeds_seen": dict(self.feeds_seen),
            "symbols_with_data": sum(
                1 for a in self._accumulators.values() if a.trade_count > 0),
        }

    # -- connection lifecycle -------------------------------------------

    def mark_connected(self, *, now=None):
        current = now or datetime.now(timezone.utc)
        if self.disconnected_at is not None:
            # The gap is RECORDED, never backfilled. Inventing empty bars
            # to make the series look continuous would turn missing data
            # into the statement "nothing traded", which is the one
            # reading it must never have.
            self.gaps.append({
                "from": self.disconnected_at.isoformat(),
                "to": current.isoformat(),
                "seconds": (current - self.disconnected_at).total_seconds(),
                "kind": "DATA_GAP",
            })
            self.disconnected_at = None
        self.connected_at = current

    def mark_disconnected(self, *, now=None):
        self.disconnected_at = now or datetime.now(timezone.utc)

    # -- persistence -----------------------------------------------------

    def snapshot(self) -> dict:
        """Enough to rebuild the current session after a restart."""
        return {
            "version": 1,
            "accumulators": [
                {
                    "symbol": a.symbol, "session": a.session,
                    "price_volume": a.price_volume, "volume": a.volume,
                    "trade_count": a.trade_count,
                    "first_cumulative": a.first_cumulative,
                    "first_trade_size": a.first_trade_size,
                    "last_cumulative": a.last_cumulative,
                    "first_amount": a.first_amount,
                    "last_amount": a.last_amount,
                    "last_trade_at": (a.last_trade_at.isoformat()
                                      if a.last_trade_at else None),
                    "bars": [b.as_record() | {
                        "open": b.open, "high": b.high, "low": b.low,
                        "close": b.close,
                        "cumulative_first": b.cumulative_first,
                        "cumulative_last": b.cumulative_last,
                        "amount_first": b.amount_first,
                        "amount_last": b.amount_last,
                    } for b in a.ordered_bars()],
                }
                for a in self._accumulators.values()
            ],
            "gaps": list(self.gaps),
            "layout_mismatches": self.layout_mismatches,
            "dropped_unparsable": self.dropped_unparsable,
            # Instrumentation is written for the reader, and the reader
            # only ever sees the snapshot. Leaving these in memory made
            # them invisible to the one audience they exist for.
            "feed_lag_samples": list(self.feed_lag.samples()),
            "feeds_seen": dict(self.feeds_seen),
        }

    @classmethod
    def restore(cls, payload, *, stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS):
        store = cls(stale_after_seconds=stale_after_seconds)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return store
        for entry in payload.get("accumulators") or ():
            accumulator = SessionAccumulator(
                symbol=entry["symbol"], session=entry["session"],
                price_volume=float(entry.get("price_volume") or 0.0),
                volume=float(entry.get("volume") or 0.0),
                trade_count=int(entry.get("trade_count") or 0),
                first_cumulative=entry.get("first_cumulative"),
                first_trade_size=entry.get("first_trade_size"),
                last_cumulative=entry.get("last_cumulative"),
                first_amount=entry.get("first_amount"),
                last_amount=entry.get("last_amount"),
                last_trade_at=_parse_iso(entry.get("last_trade_at")))
            for raw in entry.get("bars") or ():
                minute = _parse_iso(raw["minute"])
                accumulator.bars[minute] = Bar(
                    symbol=entry["symbol"], session=entry["session"],
                    minute=minute, open=raw["open"], high=raw["high"],
                    low=raw["low"], close=raw["close"],
                    volume=raw["volume"], trade_count=raw["trade_count"],
                    first_trade_at=_parse_iso(raw["first_trade_at"]),
                    last_trade_at=_parse_iso(raw["last_trade_at"]),
                    cumulative_first=raw.get("cumulative_first"),
                    cumulative_last=raw.get("cumulative_last"),
                    amount_first=raw.get("amount_first"),
                    amount_last=raw.get("amount_last"))
            store._accumulators[(accumulator.symbol, accumulator.session)] = accumulator
        store.gaps = list(payload.get("gaps") or ())
        store.layout_mismatches = int(payload.get("layout_mismatches") or 0)
        store.dropped_unparsable = int(payload.get("dropped_unparsable") or 0)
        store.feed_lag.restore(payload.get("feed_lag_samples") or ())
        store.feeds_seen = dict(payload.get("feeds_seen") or {})
        return store


def _parse_iso(text):
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
