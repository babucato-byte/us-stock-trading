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

#: Ceiling on the adaptive interval. Beyond this a pass would not finish
#: inside its hourly window, and a manifest that arrives after the next
#: one was due is worth less than a partial one that arrives on time.
MAX_PAUSE_SECONDS = 20.0

#: Provider worker threads. Bounded rather than `threads=True`, which
#: lets the library size the pool from the batch: an unbounded pool over
#: a long pass produced "can't start new thread" and getaddrinfo
#: failures, which are local-resource faults that look exactly like data
#: faults in the results.
MAX_WORKERS = 8
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


def acceptable_bar_dates(trading_day, session=None) -> set:
    """Which daily-bar dates count as current for this session.

    REGULAR and later sessions have today's bar once trading has begun,
    so today is accepted. Every session also accepts the last COMPLETED
    session's bar, because that is the correct liquidity baseline before
    today's exists -- and before the open it is the ONLY thing that
    exists.

    Resolved through the trading calendar, never by subtracting a day:
    Monday premarket must baseline on Friday, and the day after a
    holiday on the last session that actually traded.
    """
    from scanners.base.trading_calendar import previous_trading_day

    dates = {str(trading_day)}
    try:
        dates.add(str(previous_trading_day(trading_day)))
    except Exception:  # noqa: BLE001 - a calendar failure must not empty
        pass                                          # the acceptable set
    return dates


def fetch_today(symbols, *, trading_day, download=None,
                batch_size=BATCH_SIZE, pause=BATCH_PAUSE_SECONDS,
                backoff=RETRY_BACKOFF_SECONDS, session=None,
                max_rounds=MAX_BATCH_RETRIES) -> Dict[str, Dict[str, float]]:
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
                               progress=False, threads=MAX_WORKERS)

    out: Dict[str, Dict[str, float]] = {}
    acceptable = acceptable_bar_dates(trading_day, session)
    pending = list(symbols)

    # Retried on the MEASURED ABSENCE of rows, not on an exception.
    #
    # The provider catches its own rate limit, logs it, and returns a
    # frame with those tickers empty -- so a full pass produced 78
    # YFRateLimitError lines and zero exceptions, and an
    # except-clause-shaped retry was dead code that never once ran while
    # coverage sat at 30%. What a caller can actually observe is which
    # symbols came back without a bar, so that is what drives the retry.
    #
    # A symbol still missing after the last round is genuinely absent
    # today -- delisted, halted, or never traded -- which is a different
    # finding from throttled and is what `coverage` then reports.
    for round_number in range(1, int(max_rounds) + 1):
        if not pending:
            break
        if round_number > 1:
            wait = float(backoff) * (round_number - 1)
            logger.info("market scan: %d symbols returned no bar; retrying "
                        "after %.0fs (round %d)", len(pending), wait,
                        round_number)
            time.sleep(wait)

        batches = list(_chunks(pending, batch_size))
        missing: List[str] = []
        # Adaptive, because the degradation is not linear in pass length:
        # 800 symbols priced 89% and 2,000 priced 91%, both at a fixed
        # pause, while 12,886 priced 57%. The provider tolerates a burst
        # and then throttles a sustained one, so a pause chosen for a
        # short pass is the wrong pause for a long one -- and a pass
        # that starts fine and degrades halfway is exactly what a fixed
        # interval cannot notice.
        #
        # An empty batch is the observable signal, the same one the
        # retry keys off. Consecutive empties widen the interval; a
        # batch that comes back normally narrows it again, so a single
        # unlucky batch does not slow the rest of the pass.
        current_pause = float(pause)
        empty_streak = 0
        for index, batch in enumerate(batches):
            if index:
                time.sleep(current_pause)
            try:
                bundle = download(batch)
            except Exception:  # noqa: BLE001 - one batch must not end the scan
                logger.warning("market scan: batch %d/%d of %d raised",
                               index + 1, len(batches), len(batch),
                               exc_info=True)
                missing.extend(batch)
                continue
            found = 0
            for symbol in batch:
                row = _measure(bundle, symbol, trading_day,
                              acceptable)
                if row is None:
                    missing.append(symbol)
                else:
                    out[symbol] = row
                    found += 1

            if found == 0 and batch:
                empty_streak += 1
                current_pause = min(current_pause * 2 or 1.0, MAX_PAUSE_SECONDS)
                if empty_streak == 1:
                    logger.info("market scan: a batch came back empty; "
                                "widening the interval to %.1fs", current_pause)
            elif found:
                empty_streak = 0
                current_pause = max(float(pause), current_pause / 2)
        pending = missing
    return out


def _measure(bundle, symbol, trading_day, acceptable_dates=None
             ) -> Optional[Dict[str, float]]:
    """One symbol's numbers from a batch, or None if it has no usable bar.

    `acceptable_dates` is the set of bar dates that count as CURRENT for
    the session being scanned, and it is the whole point of this
    signature. The check used to be `bar date == trading_day`, which
    silently conflated three different things:

        trading_day             the operational US trading day
        baseline bar date       the last COMPLETED session's bar
        current-session data    what is happening right now

    Those coincide during REGULAR and diverge everywhere else. Between
    the ET midnight rollover and the open, no symbol has a bar for the
    new trading day, so the equality discarded the entire universe: a
    05:12 ET run priced 0 of 5,624, wrote an empty manifest over a good
    600-symbol one, and the trading node fell back to its 300-name
    server ranking for the rest of the night. The candidates never
    failed a gate; they stopped being offered.

    The original intent stands and is preserved: a bar older than the
    acceptable set is still refused, because ranking a name on last
    week's volume is exactly the failure this scan exists to remove.
    What changes is that "acceptable" is now decided by the session
    rather than assumed to be today.
    """
    frame = _frame_for(bundle, symbol)
    if frame is None or len(frame) == 0:
        return None
    try:
        stamp = str(frame.index[-1])[:10]
        allowed = acceptable_dates or {str(trading_day)}
        if stamp not in allowed:
            return None
        last = frame.iloc[-1]
        price = float(last["Close"])
        volume = float(last["Volume"])
    except Exception:  # noqa: BLE001
        return None
    if not (price > 0 and volume > 0):
        return None
    average = None
    try:
        prior = frame.iloc[:-1]
        if len(prior):
            average = float(prior["Volume"].mean())
    except Exception:  # noqa: BLE001
        average = None
    return {
        "bar_date": stamp,
        "price": price,
        "volume": volume,
        "dollar_volume": price * volume,
        "avg_volume": average,
        "relative_volume": (volume / average
                            if average and average > 0 else None),
    }


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
        min_price=MIN_PRICE, min_dollar_volume=MIN_DOLLAR_VOLUME,
        pause=BATCH_PAUSE_SECONDS, backoff=RETRY_BACKOFF_SECONDS,
        max_rounds=MAX_BATCH_RETRIES, raw_universe_count=None
        ) -> Dict[str, Any]:
    """One first-stage scan. Returns the manifest document."""
    from discovery import manifest as manifest_module
    from discovery import provider_health

    started = time.monotonic()
    scan_id = f"{trading_day}-{session}-{uuid.uuid4().hex[:8]}"
    universe = list(symbols)

    # Counted from the provider's own log, which is the only place the
    # difference between "throttled" and "delisted" exists.
    with provider_health.capture() as failures:
        measured = fetch_today(universe, trading_day=trading_day,
                               download=download, pause=pause,
                               backoff=backoff, max_rounds=max_rounds,
                               session=session)
    failure_counts = failures.summary()
    rows = rank(measured, min_price=min_price,
                min_dollar_volume=min_dollar_volume,
                max_symbols=max_symbols)
    duration = round(time.monotonic() - started, 3)

    # The oldest bar any measurement actually rested on. Recorded rather
    # than assumed: before the open every row comes from the previous
    # completed session, and a reader must be able to see that without
    # inferring it from the clock.
    bar_dates = {row.get("bar_date") for row in measured.values()
                 if row.get("bar_date")}
    baseline_date = min(bar_dates) if bar_dates else None
    missing = [s for s in universe if s not in measured]
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
        raw_universe_count=raw_universe_count,
        baseline_daily_bar_date=baseline_date,
        failed_count=len(missing),
        failure_reason_counts=failure_counts,
        generated_at=datetime.now(timezone.utc).isoformat())
