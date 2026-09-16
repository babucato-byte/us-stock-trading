#!/usr/bin/env python3
"""Run the SAME S6 evaluation twice -- once per provider -- and diff it.

Why a harness and not a second scanner
--------------------------------------
The question this answers is "would the broad provider have found what
KIS found", and the only way that answer means anything is if the
strategy code on both sides is byte-identical. So this imports the real
`build_features` and the real `OpeningRangeBreakoutScanner.evaluate`, and
the ONLY thing that differs between the two runs is which provider object
produced the bars. There is no parallel implementation of a gate, a
range, a score or a rank here, and there must never be: a parity report
computed from a reimplementation measures the reimplementation.

The metric that matters
-----------------------
FALSE NEGATIVES -- a symbol KIS qualified that the broad provider did
not. A false positive costs one wasted evaluation downstream, where the
existing precision and risk gates still stand between it and an order. A
false negative is a trade that silently stops happening, with nothing in
any log to say a candidate was ever missed. They are not symmetric and
this report does not average them.

What this harness will NOT do
-----------------------------
Publish, order, write a candidate manifest, or touch the active-watch
store. It calls `evaluate`, not `evaluate_into`, precisely so it cannot
reach `_admit_s6_pass_provisionally`, which the runner's PASS path uses
to admit a symbol into S6's live watch tier. A shadow comparison that
could put a symbol in front of the entry path would not be a shadow.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("shadow_parity")

SUPPORTED_SESSIONS = ("PREMARKET", "AFTER_HOURS")

#: The provider key production's extended-session pool is stored under.
#: Matches `KISBarMarketDataProvider.provider_name`, because that is what
#: the live scan passes to `ActivityStore.load`.
ACTIVE_POOL_PROVIDER_KEY = "kis"

#: Only a default for the ACTIVE pool when no `--limit` is given -- the
#: pool itself is whatever the ranking holds. This is not a universe size
#: and nothing downstream may treat it as one.
DEFAULT_ACTIVE_POOL = 600

#: How far behind "now" the shadow window must END.
#:
#: Not a tuning knob -- an entitlement boundary. This account holds SIP
#: HISTORICAL only, and asking for a window that reaches into the last
#: ~15 minutes is REFUSED outright: `{"message": "subscription does not
#: permit querying recent SIP data"}`. Measured 15.3-17.3 min across
#: AAPL/MSFT/NVDA/TSLA on 2026-09-16, so 20 clears it with margin.
#:
#: The consequence is stated rather than worked around: parity is
#: measured on [origin, now - lag], and the authoritative side is cut to
#: the SAME window before diffing. Comparing KIS's full session against
#: a shadow window 20 minutes shorter would report the shadow missing
#: every minute in the tail, which is an artifact of the subscription
#: and not a property of the feed.
ENTITLEMENT_LAG_MINUTES = 20


def _providers(session, symbols, *, window, trading_day):
    """The authoritative provider and the shadow one, both ready to ask."""
    from market_data.alpaca_bar_provider import broad_provider_for_session
    from market_data.kis_bar_provider import provider_for_session
    from scanners.base.market_data_provider import default_provider

    base = default_provider()

    from brokers.kis_broker import KISBroker

    authoritative = provider_for_session(session, broker=KISBroker(),
                                         fallback=base, trading_day=trading_day)

    shadow = broad_provider_for_session(session, fallback=base, window=window)
    if shadow is None:
        raise SystemExit(f"no broad provider applies to session {session!r}")
    prime_stats = shadow.prime(symbols)
    return authoritative, shadow, prime_stats


def _evaluate(provider, scanner, symbol, *, session, trading_day):
    """One symbol through the real feature + strategy path.

    Returns (signal, features, frame, error). An error is returned rather
    than raised because one symbol failing on one side is a DATA point
    for the comparison -- it is very often the entire finding.
    """
    from scanners.base.features import build_features

    try:
        bundle = provider.get_symbol_data(
            symbol, intraday_interval="1m", intraday_lookback_days=2)
    except Exception as exc:  # noqa: BLE001 - one symbol, not the sweep
        return (None, None, None, f"{type(exc).__name__}: {exc}")

    frame = bundle.intraday
    try:
        features = build_features(bundle)
    except Exception as exc:  # noqa: BLE001
        return (None, None, frame, f"features: {type(exc).__name__}: {exc}")

    try:
        signal = scanner.evaluate(bundle, trading_day=trading_day,
                                  shared_features=features, session=session)
    except Exception as exc:  # noqa: BLE001
        return (None, features, frame, f"evaluate: {type(exc).__name__}: {exc}")
    return (signal, features, frame, None)


def _bar_diff(a, b, *, window=None):
    """How two frames for one symbol differ, minute by minute.

    Both sides are cut to `window` first. The authoritative provider sees
    the live session and the shadow cannot (see ENTITLEMENT_LAG_MINUTES),
    so an uncut diff would attribute the subscription's lag to the feed.
    """
    if window is not None:
        a = _cut(a, window)
        b = _cut(b, window)
    if a is None or b is None:
        return {"comparable": False,
                "kis_bars": None if a is None else len(a),
                "broad_bars": None if b is None else len(b)}
    left = {ts: row for ts, row in a.iterrows()}
    right = {ts: row for ts, row in b.iterrows()}
    shared = sorted(set(left) & set(right))
    close_gaps = []
    volume_gaps = []
    for ts in shared:
        try:
            lc, rc = float(left[ts]["Close"]), float(right[ts]["Close"])
            if lc and abs(lc - rc) / lc > 0.001:      # >10bp
                close_gaps.append(ts.isoformat())
            lv, rv = float(left[ts]["Volume"]), float(right[ts]["Volume"])
            if lv and abs(lv - rv) / lv > 0.05:       # >5%
                volume_gaps.append(ts.isoformat())
        except (KeyError, TypeError, ValueError):
            continue
    return {
        "comparable": True,
        "kis_bars": len(left), "broad_bars": len(right),
        "shared_minutes": len(shared),
        "only_in_kis": len(set(left) - set(right)),
        "only_in_broad": len(set(right) - set(left)),
        "close_mismatches_over_10bp": len(close_gaps),
        "volume_mismatches_over_5pct": len(volume_gaps),
        "close_mismatch_sample": close_gaps[:3],
    }


def _cut(frame, window):
    """`frame` restricted to [start, end]. None stays None."""
    if frame is None or len(frame) == 0:
        return frame
    start, end = window
    try:
        return frame[(frame.index >= start) & (frame.index <= end)]
    except TypeError:      # a naive index cannot be compared to an aware bound
        return frame


def _feature_diff(a, b):
    """The specific numbers §8 names."""
    if a is None or b is None:
        return None
    fields = ("price", "vwap", "ema9", "ema21", "avg_volume",
              "volume_multiple", "extension_pct")
    out = {}
    for field in fields:
        left = getattr(a, field, None)
        right = getattr(b, field, None)
        if left is None or right is None:
            out[field] = {"kis": left, "broad": right, "delta_pct": None}
            continue
        try:
            delta = (abs(float(left) - float(right)) / abs(float(left)) * 100.0
                     if float(left) else None)
        except (TypeError, ValueError, ZeroDivisionError):
            delta = None
        out[field] = {"kis": _r(left), "broad": _r(right), "delta_pct": _r(delta)}
    return out


def _r(value, places=6):
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, choices=SUPPORTED_SESSIONS)
    parser.add_argument("--symbols", help="comma-separated; overrides --universe")
    parser.add_argument("--universe", default="active",
                        choices=("active", "manifest", "full"))
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the universe for a smoke run; NOT a fixed "
                             "size -- omit it for the real comparison")
    parser.add_argument("--out", default=None, help="write the JSON report here")
    parser.add_argument("--end-lag-minutes", type=int,
                        default=ENTITLEMENT_LAG_MINUTES,
                        help="how far behind now the compared window ends; "
                             "an entitlement boundary, not a preference")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    session = args.session
    now = datetime.now(timezone.utc)

    from config.operational_calendar import operational_trading_day
    from market_hours import us_trading_day
    from scanners.base import session_range as srange

    day = operational_trading_day(now) or us_trading_day(now)
    session_date = srange.current_session_date(session, now)
    origin = srange.official_origin(session, session_date)
    if origin is None:
        raise SystemExit(f"no official origin for {session} on {session_date}")

    # The shadow provider fetches BY TIME, so it is given the session's
    # real window -- from the official origin to now. Not a day count: a
    # window that wraps midnight is why `session_range` exists.
    fetch_start = origin
    fetch_end = now - timedelta(minutes=args.end_lag_minutes)
    if fetch_end <= fetch_start:
        raise SystemExit(
            f"{session} has not been open longer than the "
            f"{args.end_lag_minutes}-minute entitlement lag yet; there is no "
            "comparable window")

    symbols = _resolve_symbols(args, session=session, day=day)
    if not symbols:
        raise SystemExit("empty universe; nothing to compare")
    logger.info("comparing %d symbols for %s (origin %s)",
                len(symbols), session, origin.isoformat())

    from scanners.registry import build_scanner

    authoritative, shadow, prime_stats = _providers(
        session, symbols, window=(fetch_start, fetch_end), trading_day=day)

    # ONE scanner instance, used for both sides. Two instances would be
    # two configs the moment anything reads a default lazily.
    scanner = build_scanner("orb")

    rows = []
    kis_positive, broad_positive = set(), set()
    for symbol in symbols:
        k_sig, k_feat, k_frame, k_err = _evaluate(
            authoritative, scanner, symbol, session=session, trading_day=day)
        b_sig, b_feat, b_frame, b_err = _evaluate(
            shadow, scanner, symbol, session=session, trading_day=day)

        if k_sig is not None:
            kis_positive.add(symbol)
        if b_sig is not None:
            broad_positive.add(symbol)

        rows.append({
            "symbol": symbol,
            "kis_signal": k_sig is not None,
            "broad_signal": b_sig is not None,
            "kis_error": k_err,
            "broad_error": b_err,
            "kis_score": _r(getattr(k_sig, "score", None)) if k_sig else None,
            "broad_score": _r(getattr(b_sig, "score", None)) if b_sig else None,
            "bars": _bar_diff(k_frame, b_frame,
                              window=(fetch_start, fetch_end)),
            "features": _feature_diff(k_feat, b_feat),
        })

    true_positive = kis_positive & broad_positive
    false_negative = kis_positive - broad_positive
    false_positive = broad_positive - kis_positive
    recall = (len(true_positive) / len(kis_positive)) if kis_positive else None

    report = {
        "generated_at": now.isoformat(),
        "session": session,
        "session_date": str(session_date),
        "official_origin": origin.isoformat(),
        "compared_window": {"start": fetch_start.isoformat(),
                            "end": fetch_end.isoformat(),
                            "end_lag_minutes": args.end_lag_minutes,
                            "why": "SIP historical-only entitlement"},
        "universe_size": len(symbols),
        "universe_source": "explicit" if args.symbols else args.universe,
        "authoritative_provider": getattr(authoritative, "provider_name", "?"),
        "shadow_provider": getattr(shadow, "provider_name", "?"),
        "shadow_feed": getattr(shadow, "feed_name", None),
        "shadow_prime": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                         for k, v in prime_stats.items()},
        "shadow_covers_origin": shadow.covers_origin(origin),
        "KIS_POSITIVES": len(kis_positive),
        "BROAD_POSITIVES": len(broad_positive),
        "TRUE_POSITIVES": len(true_positive),
        "FALSE_POSITIVES": len(false_positive),
        "FALSE_NEGATIVES": len(false_negative),
        "RECALL": _r(recall, 4),
        "false_negative_symbols": sorted(false_negative),
        "false_positive_symbols": sorted(false_positive),
        "rows": rows,
    }

    text = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text)
        logger.info("wrote %s", args.out)
    else:
        print(text)

    logger.info("KIS+=%d BROAD+=%d TP=%d FP=%d FN=%d RECALL=%s",
                len(kis_positive), len(broad_positive), len(true_positive),
                len(false_positive), len(false_negative), report["RECALL"])
    return 0


def _resolve_symbols(args, *, session, day):
    """The universe, from the flow that already owns it.

    No count is hardcoded here. `--limit` exists for smoke runs and is
    explicitly not a universe size -- the real comparison omits it and
    takes whatever today's discovery produced.
    """
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if args.universe == "full":
        from scanners.universe import load_symbols

        return load_symbols(limit=args.limit)

    if args.universe == "manifest":
        from discovery import manifest as manifest_module
        from scanners.runner import MANIFEST_DEFAULT_PATH

        verdict = manifest_module.validate(
            manifest_module.read(MANIFEST_DEFAULT_PATH), trading_day=day)
        if verdict["status"] in (manifest_module.VALID, manifest_module.PARTIAL):
            found = [str(r["symbol"]).upper() for r in verdict["symbols"]]
            return found[:args.limit] if args.limit else found
        logger.warning("manifest unusable (%s); falling back to the active "
                       "ranking", verdict["status"])

    from scanners.base import activity as act
    from scanners.base import eligibility as elig
    from scanners.base import universe_selection as universe_sel

    # Read under the AUTHORITATIVE provider's key, not a key of this
    # harness's own. Both stores are per-provider, so a fresh key would
    # return an empty ranking and this would silently compare the two
    # providers on a universe production never scans.
    pool_key = ACTIVE_POOL_PROVIDER_KEY
    activity = act.ActivityStore.load(pool_key)
    eligibility = elig.EligibilityStore.load(pool_key)
    selection = universe_sel.eligible_top(
        activity, eligibility, limit=args.limit or DEFAULT_ACTIVE_POOL)
    return list(selection.symbols)


if __name__ == "__main__":
    sys.exit(main())
