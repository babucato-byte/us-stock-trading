"""First-stage discovery over the whole US market, on TODAY's data.

What changed and why
--------------------
S6's candidate universe was the previous day's dollar-volume top 300.
That is a ranking built after the previous close, so on a Monday it is
Friday's -- and a name whose volume arrived this morning was not in it.
Over several sessions the observed consequence was not a few missed
candidates but almost none at all: the discovery stage, not the strategy
gates, was the binding constraint.

So the first stage now starts from the full tradeable universe and ranks
it on data from today. Measured cost at batch 400: about 0.036s per
symbol, so ~12,900 names is roughly six or seven minutes -- affordable
once an hour, which is the cadence this runs at. Per-tick would not be,
and is not attempted.

This stage is not the strategy
------------------------------
It answers one question: which symbols are worth a precision S6
evaluation. It ranks by liquidity, because an illiquid name cannot be
entered or exited at qty 1 with a limit order regardless of its chart.
It deliberately does NOT rank by today's price move: the day's gain is
what S6's own gates are there to judge, and pre-ranking on it would
smuggle a momentum filter in ahead of the opening-range test that is
supposed to make that call.

Nothing here is a buy signal. This node holds no broker credentials, and
the trading node re-derives every strategy condition from its own market
data before an order is considered.
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Symbols per provider call. 400 was fastest per symbol in isolation
#: and is NOT what a full pass can sustain: a 12,886-name run at 400
#: priced only 4,038 (31%) before the provider answered
#: YFRateLimitError for the rest. A manifest built from whichever third
#: arrived first is not "the market's top 600" -- it is the top 600 of
#: an arbitrary sample, and it would carry that bias silently.
BATCH_SIZE = 150

#: Seconds to wait between batches. The limiter is per-window, not
#: per-request, so pacing is what keeps a long pass inside it; going
#: faster does not finish sooner when the tail is refused.
BATCH_PAUSE_SECONDS = 1.5

#: A rate-limited batch is retried, because "we were throttled" and
#: "these symbols do not trade" are opposite findings and the ranking
#: cannot tell them apart afterwards.
MAX_BATCH_RETRIES = 3
RETRY_BACKOFF_SECONDS = 20.0

#: Below this fraction of the universe priced, the pass is reported as
#: PARTIAL. The trading node then knows the ranking it is reading was
#: drawn from a sample rather than the market.
MIN_COVERAGE_FOR_COMPLETE = 0.80

#: A share the account cannot buy whole at qty 1 is not worth a
#: precision scan. Not a strategy threshold -- an execution fact.
MIN_PRICE = 1.0

#: Below this a limit order at qty 1 is not reliably fillable.
MIN_DOLLAR_VOLUME = 5_000_000.0

#: Safety cap on the manifest. Deliberately not 300: that number was the
#: old previous-day pool size and reusing it would import a limit that
#: was never chosen for this stage. Sized so the trading node's 15-minute
#: precision scan stays inside its window, and revisited from the
#: measurements this scan records.
DEFAULT_MAX_SYMBOLS = 600


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _frame_for(bundle, symbol):
    try:
        sub = bundle[symbol]
    except Exception:  # noqa: BLE001
        return None
    try:
        return sub.dropna(how="all")
    except Exception:  # noqa: BLE001
        return None


def fetch_today(symbols, *, trading_day, download=None,
                batch_size=BATCH_SIZE) -> Dict[str, Dict[str, float]]:
    """Today's price, volume and relative volume, per symbol.

    Five days are requested rather than one so relative volume comes
    from the same call: a second pass for the average would double the
    cost of the only expensive step here.

    A symbol whose latest bar is not today's is omitted entirely rather
    than carried with stale numbers -- ranking a name on Friday's volume
    is the exact failure this scan exists to remove.
    """
    if download is None:
        import yfinance as yf

        def download(tickers):
            return yf.download(" ".join(tickers), period="5d", interval="1d",
                               group_by="ticker", auto_adjust=False,
                               progress=False, threads=True)

    out: Dict[str, Dict[str, float]] = {}
    batches = list(_chunks(list(symbols), batch_size))
    for index, batch in enumerate(batches):
        if index:
            time.sleep(BATCH_PAUSE_SECONDS)
        bundle = None
        for attempt in range(1, MAX_BATCH_RETRIES + 1):
            try:
                bundle = download(batch)
                break
            except Exception as exc:  # noqa: BLE001 - one batch must not end the scan
                throttled = "ratelimit" in type(exc).__name__.lower() \
                    or "too many requests" in str(exc).lower()
                if throttled and attempt < MAX_BATCH_RETRIES:
                    wait = RETRY_BACKOFF_SECONDS * attempt
                    logger.warning("market scan: throttled on batch %d/%d, "
                                   "waiting %.0fs (attempt %d)",
                                   index + 1, len(batches), wait, attempt)
                    time.sleep(wait)
                    continue
                logger.warning("market scan: batch %d/%d of %d failed (%s)",
                               index + 1, len(batches), len(batch),
                               type(exc).__name__)
                break
        if bundle is None:
            continue
        for symbol in batch:
            frame = _frame_for(bundle, symbol)
            if frame is None or len(frame) == 0:
                continue
            try:
                stamp = str(frame.index[-1])[:10]
                if stamp != str(trading_day):
                    continue                      # not today's bar
                row = frame.iloc[-1]
                price = float(row["Close"])
                volume = float(row["Volume"])
            except Exception:  # noqa: BLE001
                continue
            if not (price > 0 and volume > 0):
                continue
            prior = frame.iloc[:-1]
            average = None
            try:
                if len(prior):
                    average = float(prior["Volume"].mean())
            except Exception:  # noqa: BLE001
                average = None
            out[symbol] = {
                "price": price,
                "volume": volume,
                "dollar_volume": price * volume,
                "avg_volume": average,
                "relative_volume": (volume / average
                                    if average and average > 0 else None),
            }
    return out


def rank(measured, *, min_price=MIN_PRICE,
         min_dollar_volume=MIN_DOLLAR_VOLUME,
         max_symbols=DEFAULT_MAX_SYMBOLS) -> List[Dict[str, Any]]:
    """Liquidity-ranked rows, capped, with the reason each one passed.

    Ranked on today's dollar volume. `relative_volume` is recorded but
    is NOT the sort key: a thin name that traded ten times its usual
    nothing is still thin, and would occupy a slot that a genuinely
    tradeable name needs.
    """
    passed = []
    for symbol, row in measured.items():
        if row["price"] < float(min_price):
            continue
        if row["dollar_volume"] < float(min_dollar_volume):
            continue
        passed.append((row["dollar_volume"], symbol, row))
    passed.sort(key=lambda item: item[0], reverse=True)

    rows = []
    for position, (_dv, symbol, row) in enumerate(passed[:int(max_symbols)],
                                                  start=1):
        relative = row.get("relative_volume")
        reason = "TODAY_DOLLAR_VOLUME"
        if relative is not None and relative >= 2.0:
            # Recorded as a label, never as a ranking bonus.
            reason = "TODAY_DOLLAR_VOLUME+VOLUME_ACCELERATION"
        rows.append({
            "symbol": symbol,
            "rank": position,
            "observed_price": round(row["price"], 4),
            "volume": int(row["volume"]),
            "dollar_volume": round(row["dollar_volume"], 2),
            "relative_volume": (round(relative, 3)
                                if relative is not None else None),
            "first_stage_reason": reason,
        })
    return rows


def run(symbols, *, trading_day, session, scanner_commit=None,
        download=None, max_symbols=DEFAULT_MAX_SYMBOLS,
        min_price=MIN_PRICE, min_dollar_volume=MIN_DOLLAR_VOLUME
        ) -> Dict[str, Any]:
    """One full-market first-stage scan. Returns the manifest document."""
    from discovery import manifest as manifest_module

    started = time.monotonic()
    scan_id = f"{trading_day}-{session}-{uuid.uuid4().hex[:8]}"
    universe = list(symbols)
    measured = fetch_today(universe, trading_day=trading_day,
                           download=download)
    rows = rank(measured, min_price=min_price,
                min_dollar_volume=min_dollar_volume,
                max_symbols=max_symbols)
    duration = round(time.monotonic() - started, 3)

    coverage = (len(measured) / len(universe)) if universe else 0.0
    complete = coverage >= MIN_COVERAGE_FOR_COMPLETE
    if not complete:
        # Said out loud. A ranking drawn from a third of the market is
        # not the market's ranking, and the trading node has to be able
        # to tell the difference -- a quiet "top 600" would carry the
        # sampling bias with no way to see it.
        logger.warning("market scan PARTIAL: only %.0f%% of the universe was "
                       "priced; the ranking is drawn from a sample",
                       coverage * 100)
    logger.info("market scan: universe=%s priced=%s (%.0f%%) passed=%s in %.1fs",
                len(universe), len(measured), coverage * 100, len(rows), duration)
    return manifest_module.build(
        trading_day=trading_day, session=session, symbols=rows,
        scanner_commit=scanner_commit, scan_id=scan_id,
        universe_size=len(universe), evaluated=len(measured),
        duration_seconds=duration, coverage=round(coverage, 4),
        complete=complete,
        generated_at=datetime.now(timezone.utc).isoformat())
